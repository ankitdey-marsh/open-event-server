import logging
import string
import secrets
from dataclasses import dataclass
from typing import Optional
 
import requests

from app.api.helpers.db import get_new_identifier, get_or_create
from app.models import db
from app.models.event import Event
from app.models.microlocation import Microlocation
from app.models.user import User
from app.models.video_stream import VideoStream
from app.settings import get_settings

from typing import Union

logger = logging.getLogger(__name__)


class RocketChatException(ValueError):
    class CODES:
        DISABLED = 'integration_disabled'
        UNHANDLED = 'unhandled'

    code = None
    response = None

    def __init__(self, message, code=CODES.UNHANDLED, response=None) -> None:
        self.code = code
        self.response = response
        super().__init__(message)


@dataclass
class RocketChat:
    api_url: str

    @property
    def login_url(self) -> str:
        return self.api_url + '/api/v1/login'

    def login(self, user: User, event: Optional[Event] = None, method: str = 'login') -> Union[dict, None]:
        def save_token(token)->None:
            user.rocket_chat_token = token
            db.session.add(user)
            db.session.commit()

        res = requests.post(
            self.login_url,
            json=dict(email=user.email, password=user.rocket_chat_password),
        )
        data = res.json()
        if res.status_code == 200:
            token = data['data']['authToken']
            save_token(token)
            if event:
                self.add_in_room(event, data['data']['userId'])
            return dict(method=method, token=token, res=data)
        else:
            # Unhandled Case
            logger.error('Error while rocket chat login: %s', data)
            raise RocketChatException('Error while logging in', response=res)

    def register(
        self,
        user: User,
        event: Optional[Event] = None,
        username_suffix='',
    )->Union[dict,None]:
        settings = get_settings()
        register_url = self.api_url + '/api/v1/users.register'
        register_data = {
            'name': user.public_name or user.full_name,
            'email': user.email,
            'pass': user.rocket_chat_password,
            'username': user.rocket_chat_username + username_suffix,
        }
        if registration_secret := settings['rocket_chat_registration_secret']:
            register_data['secretURL'] = registration_secret

        res = requests.post(register_url, json=register_data)

        data = res.json()
        if res.status_code == 200:
            return self.login(user, event, 'registered')
        elif res.status_code == 400:
            if data.get('error') == 'Username is already in use':
                # Username conflict. Add random suffix and retry
                return self.register(user, event, '.' + get_new_identifier(length=5))
            logger.info('Bad Request during register: %s', data)
            # Probably already registered. Try logging in
            return self.login(user, event, 'login')
        else:
            logger.error(
                'Error while rocket chat registration: %d %s',
                res.status_code,
                data,
            )
            raise RocketChatException('Error while registration', response=res)

    def get_token(
        self,
        user: User,
        event: Optional[Event] = None,
        retried=False,
        microlocation: Optional[Microlocation] = None,
    )->Union[dict,None]:
        if user.rocket_chat_token:
            res = requests.post(self.login_url, json=dict(resume=user.rocket_chat_token))

            data = res.json()
            if res.status_code == 200:
                if event:
                    self.add_in_room(event, data['data']['userId'], microlocation)
                return dict(method='resumed', token=user.rocket_chat_token, res=data)
            elif res.status_code == 401:
                # Token Expired. Login again

                try:
                    return self.login(user, event, 'login')
                except RocketChatException as rce:
                    if (
                        not retried
                        and rce.response is not None
                        and rce.response.status_code == 401
                    ):
                        # Invalid credentials stored. Reset credentials and retry
                        # If we have already retried, give up
                        user.rocket_chat_token = None
                        db.session.add(user)
                        db.session.commit()
                        return self.get_token(user, event, retried=True)
                    else:
                        raise rce
            else:
                # Unhandled Case
                logger.error('Error while rocket chat resume or login: %s', data)
                raise RocketChatException(
                    'Error while resume or logging in', response=res
                )
        else:
            # No token. Try creating profile, else login

            return self.register(user, event)

    def get_token_virtual_room(
        self,
        user: User,
        event: Optional[Event] = None,
        retried=False,
        videoStream: Optional[VideoStream] = None,
    )->Union[dict,None]:
        if user.rocket_chat_token:
            res = requests.post(self.login_url, json=dict(resume=user.rocket_chat_token))

            data = res.json()
            if res.status_code == 200:
                if event:
                    self.add_in_room_virtual_room(
                        event, data['data']['userId'], videoStream
                    )
                return dict(method='resumed', token=user.rocket_chat_token, res=data)
            elif res.status_code == 401:
                # Token Expired. Login again

                try:
                    return self.login(user, event, 'login')
                except RocketChatException as rce:
                    if (
                        not retried
                        and rce.response is not None
                        and rce.response.status_code == 401
                    ):
                        # Invalid credentials stored. Reset credentials and retry
                        # If we have already retried, give up
                        user.rocket_chat_token = None
                        db.session.add(user)
                        db.session.commit()
                        return self.get_token_virtual_room(user, event, retried=True)
                    else:
                        raise rce
            else:
                # Unhandled Case
                logger.error('Error while rocket chat resume or login: %s', data)
                raise RocketChatException(
                    'Error while resume or logging in', response=res
                )
        else:
            # No token. Try creating profile, else login

            return self.register(user, event)

    def check_or_create_bot(self)->User:
        bot_email = 'open-event-bot@open-event.invalid'
        bot_user, _ = get_or_create(
            User,
            _email=bot_email,
            defaults=dict(password=generate_pass(), first_name='open-event-bot'),
        )

        return bot_user

    def create_room(self, event: Event, microlocation: Optional[Microlocation], data)->None:
        bot_token = data['token']
        bot_id = data['res']['data']['userId']
        if microlocation:
            chat_room_name = microlocation.chat_room_name
        else:
            chat_room_name = event.chat_room_name

        res = requests.post(
            self.api_url + '/api/v1/groups.create',
            json=dict(
                name=chat_room_name,
                members=[bot_id],
            ),
            headers={
                'X-Auth-Token': bot_token,
                'X-User-Id': bot_id,
            },
        )
        if not res.status_code == 200:
            logger.error('Error while creating room : %s', res.json())
            raise RocketChatException('Error while creating room', response=res)
        else:
            group_data = res.json()
            if microlocation:
                microlocation.chat_room_id = group_data['group']['_id']
                db.session.add(microlocation)
            else:
                event.chat_room_id = group_data['group']['_id']
                db.session.add(event)
            db.session.commit()

    def create_room_virtual_room(
        self, event: Event, videoStream: Optional[VideoStream], data
    )->None:
        bot_token = data['token']
        bot_id = data['res']['data']['userId']
        if videoStream:
            chat_room_name = videoStream.chat_room_name
        else:
            chat_room_name = event.chat_room_name

        res = requests.post(
            self.api_url + '/api/v1/groups.create',
            json=dict(
                name=chat_room_name,
                members=[bot_id],
            ),
            headers={
                'X-Auth-Token': bot_token,
                'X-User-Id': bot_id,
            },
        )
        if not res.status_code == 200:
            logger.error('Error while creating room : %s', res.json())
            raise RocketChatException('Error while creating room', response=res)
        else:
            group_data = res.json()
            if videoStream:
                videoStream.chat_room_id = group_data['group']['_id']
                db.session.add(videoStream)
            else:
                event.chat_room_id = group_data['group']['_id']
                db.session.add(event)
            db.session.commit()

    def add_in_room(
        self, event: Event, rocket_user_id, microlocation: Optional[Microlocation] = None
    )->None:
        bot = self.check_or_create_bot()
        data = self.get_token(bot)

        if (not event.chat_room_id) or (microlocation and not microlocation.chat_room_id):
            self.create_room(event=event, microlocation=microlocation, data=data)

        if microlocation is not None:
            chat_room_id = microlocation.chat_room_id
        else:
            chat_room_id = event.chat_room_id

        bot_token = data['token']
        bot_id = data['res']['data']['userId']
        room_info = {'roomId': chat_room_id, 'userId': rocket_user_id}

        res = requests.post(
            self.api_url + '/api/v1/groups.invite',
            json=room_info,
            headers={
                'X-Auth-Token': bot_token,
                'X-User-Id': bot_id,
            },
        )

        if res.status_code != 200:
            logger.error('Error while adding user : %s', res.json())
            raise RocketChatException('Error while adding user', response=res)

    def add_in_room_virtual_room(
        self, event: Event, rocket_user_id, videoStream: Optional[VideoStream] = None
    )->None:
        bot = self.check_or_create_bot()
        data = self.get_token_virtual_room(bot, videoStream=videoStream)
        if (not event.chat_room_id) or (videoStream and not videoStream.chat_room_id):
            self.create_room_virtual_room(event=event, videoStream=videoStream, data=data)

        if videoStream is not None:
            chat_room_id = videoStream.chat_room_id
        else:
            chat_room_id = event.chat_room_id

        bot_token = data['token']
        bot_id = data['res']['data']['userId']
        room_info = {'roomId': chat_room_id, 'userId': rocket_user_id}

        res = requests.post(
            self.api_url + '/api/v1/groups.invite',
            json=room_info,
            headers={
                'X-Auth-Token': bot_token,
                'X-User-Id': bot_id,
            },
        )

        if res.status_code != 200:
            logger.error('Error while adding user : %s', res.json())
            raise RocketChatException('Error while adding user', response=res)


def generate_pass(size=10, chars=string.digits + string.ascii_letters + string.punctuation)->str:
    # Error handled cases for negative size and no password characters.
    # Secrets used instead of random, due to the code being used by company.
    if size < 0:
        raise ValueError("Negative length for password not allowed")
    elif not chars:
        raise ValueError("Password cannot be empty")
    return ''.join(secrets.choice(chars) for _ in range(size))


def get_rocket_chat_token(
    user: User,
    event: Optional[Event] = None,
    microlocation: Optional[Microlocation] = None,
)->dict:
    settings = get_settings()
    if not (api_url := settings['rocket_chat_url']):
        raise RocketChatException(
            'Rocket Chat Integration is not enabled', RocketChatException.CODES.DISABLED
        )

    rocket_chat = RocketChat(api_url)
    return rocket_chat.get_token(user, event, microlocation=microlocation)


def get_rocket_chat_token_virtual_room(
    user: User,
    event: Optional[Event] = None,
    videoStream: Optional[VideoStream] = None,
)->Union[dict,None]:
    settings = get_settings()
    if not (api_url := settings['rocket_chat_url']):
        raise RocketChatException(
            'Rocket Chat Integration is not enabled', RocketChatException.CODES.DISABLED
        )

    rocket_chat = RocketChat(api_url)
    return rocket_chat.get_token_virtual_room(user, event, videoStream=videoStream)


def rename_rocketchat_room(event: Event)-> None:
    settings = get_settings()
    if not event.chat_room_id or not (api_url := settings['rocket_chat_url']):
        return

    rocket_chat = RocketChat(api_url)
    bot = rocket_chat.check_or_create_bot()
    data = rocket_chat.get_token(bot)

    bot_token = data['token']
    bot_id = data['res']['data']['userId']

    res = requests.post(
        rocket_chat.api_url + '/api/v1/groups.rename',
        json=dict(
            name=event.chat_room_name,
            roomId=event.chat_room_id,
        ),
        headers={
            'X-Auth-Token': bot_token,
            'X-User-Id': bot_id,
        },
    )

    if not res.status_code == 200:
        logger.error('Error while changing room name : %s', res.json())
        raise RocketChatException('Error while changing room name', response=res)
