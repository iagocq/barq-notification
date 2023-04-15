import sys
import argparse
import asyncio
import aiohttp
import shelve
from pyrogram.client import Client
from pyrogram.types import BotCommand, Message
from typing import Any
from dataclasses import dataclass
import logging

API_URL = 'https://api.barq.app/graphql'
NEWMSG = 0

@dataclass
class Chat:
    id: str
    last_msg: str

@dataclass
class RefresherData:
    chats: dict[str, Chat]
    auth: str
    running: bool
    update_data: Any
    self_id: str | None = None

@dataclass
class Refresher:
    data: RefresherData
    update_queue: asyncio.Queue
    api_url: str = API_URL

    async def run(self):
        async with aiohttp.ClientSession() as session:
            session.headers['authorization'] = f'Bearer {self.data.auth}'

            if not self.data.self_id:
                status, d = await self._api(session, '{ user { profile { id } } }')
                if status == 200 and isinstance(d, dict):
                    self.data.self_id = d['data']['user']['profile']['id']
                    print(self.data)
                else:
                    print(status, d)

            while True:
                if self.data.running:
                    await self._do_refresh(session)
                await asyncio.sleep(5)


    async def _api(self, session: aiohttp.ClientSession, query: str):
        r = await session.post(self.api_url, data={'query': query})
        try:
            data = await r.json()
        except:
            data = await r.text()
        return r.status, data

    async def _do_refresh(self, session: aiohttp.ClientSession):
        status, d = await self._api(session, (
            '{ chats { '
                'id '
                'lastMessage { profile { id displayName } content id } '
            '} }'
        ))
        if status == 200 and isinstance(d, dict):
            for chat in d['data']['chats']:
                chat_id = chat['id']
                msg = chat['lastMessage']
                msg_id = msg['id']
                content = msg['content']

                chats = self.data.chats
                if chat_id not in chats:
                    chats[chat_id] = Chat(chat_id, msg_id)

                if chats[chat_id].last_msg != msg_id:
                    chats[chat_id].last_msg = msg_id
                    sender = msg['profile']['displayName']
                    if msg['profile']['id'] == self.data.self_id: continue

                    await self.update_queue.put((self.data.update_data, NEWMSG, (sender, content)))
        else:
            print(status, d)

class Bot:
    def __init__(self, app: Client, db: shelve.Shelf) -> None:
        self._app = app
        self._db = db
        self._quit = asyncio.Event()
        if 'refreshers' not in self._db:
            self._db['refreshers'] = {}

        self._refresher_queue = asyncio.Queue()
        refreshers = {}
        for refdata in self._db['refreshers'].values():
            refreshers[refdata['update_data']] = Refresher(refdata, self._refresher_queue)

        self._refreshers: dict[int, Refresher] = refreshers
        self._tasks = []

    async def run(self):
        commands: list[BotCommand] = [
            BotCommand('start', 'start the bot and start receiving new messages'),
            BotCommand('stop', 'stop receiving new messages'),
            BotCommand('login', 'set up your credentials with the bot')
        ]

        await self._app.set_bot_commands(commands)
        self._app.on_message()(self._on_message)
        self._tasks.extend(asyncio.create_task(t) for t in [
            self._get_refresher_updates(),
            self._sync_db()
        ])
        self._tasks.extend(asyncio.create_task(r.run()) for r in self._refreshers.values())
        await self._quit.wait()

    async def _on_message(self, _, msg: Message):
        if not msg.text: return
        args = msg.text.split()
        if not args[0].startswith('/'): return

        if args[0] == '/start':
            if msg.chat.id in self._refreshers:
                self._refreshers[msg.chat.id].data.running = True
                await msg.reply('receiving messages')
            else:
                await msg.reply("you haven't logged in yet")
        if args[0] == '/login' and len(args) == 2:
            if msg.chat.id not in self._refreshers:
                data = RefresherData({}, args[1], True, msg.chat.id)
                refresher = Refresher(data, self._refresher_queue)
                self._db[str(msg.chat.id)] = data
                self._refreshers[msg.chat.id] = refresher
                self._tasks.append(asyncio.create_task(refresher.run()))
                await msg.reply('receiving messages')

        if args[0] == '/stop':
            if msg.chat.id in self._refreshers:
                self._refreshers[msg.chat.id].data.running = False
                await msg.reply('not receiving messages anymore')
            else:
                await msg.reply("you haven't logged in yet")

    async def _send_user_update(self, uid, upd_type, args):
        if upd_type == NEWMSG:
            sender, content = args
            await self._app.send_message(uid, f'{sender}:\n\n{content}')

    async def _get_refresher_updates(self):
        while not self._quit.is_set():
            uid, upd_type, args = await self._refresher_queue.get()
            try:
                await self._send_user_update(uid, upd_type, args)
            except:
                pass

    async def _sync_db(self):
        while not self._quit.is_set():
            await asyncio.sleep(600)
            self._db.sync()

async def app_main(*,
                   token: str,
                   shelve_name: str,
                   pyro_name: str,
                   api_id: str,
                   api_hash: str):
    app = Client(name=pyro_name, bot_token=token, api_id=api_id, api_hash=api_hash)
    with shelve.open(shelve_name, writeback=True) as db:
        async with app:
            await Bot(app, db).run()

def main(prog: str, argv: list[str]):
    parser = argparse.ArgumentParser(prog)
    parser.add_argument('-t', '--token',
                        required=True,
                        help='telegram bot token')
    parser.add_argument('-s', '--shelve',
                        metavar='FILE',
                        help='database file',
                        default='barqing.sv')
    parser.add_argument('-n', '--name',
                        help='pyrogram session name',
                        metavar='FILE',
                        default='barqing.tg')
    parser.add_argument('--api-id', required=True)
    parser.add_argument('--api-hash', required=True)
    parser.add_argument('-v', '--verbose', action='count', default=0)

    args = parser.parse_args(argv)

    if args.verbose > 0:
        logging.basicConfig(level=(3-args.verbose)*10)

    asyncio.run(app_main(token=args.token,
                         shelve_name=args.shelve,
                         pyro_name=args.name,
                         api_id=args.api_id,
                         api_hash=args.api_hash))

if __name__ == '__main__':
    main(sys.argv[0], sys.argv[1:])
