#!/usr/bin/env python

import os
import uuid
import json
import re
import struct
import tornado.ioloop
import tornado.web
from tornado import websocket
import logging
from tornado.log import enable_pretty_logging
from tornado.log import app_log
from tornado.escape import json_encode

from tornado.util import bytes_type
from tornado.iostream import StreamClosedError
import tornado.options
import tornado.escape

MAX_ROOMS = 100
MAX_USERS_PER_ROOM = 100
MAX_LEN_ROOMNAME = 20
MAX_LEN_NICKNAME = 20


class RoomHandler(object):
    """Store data about connections, rooms, which users are in which rooms, etc."""

    def __init__(self):
        self.client_info = {}  # for each client id we'll store  {'wsconn': wsconn, 'room':room, 'nick':nick}
        self.room_info = {}  # dict to store a list of  {'cid':cid, 'nick':nick , 'wsconn': wsconn} for each room
        self.pending_cwsconn = {}  # pending client ws connection
        self.roomates = {}  # store a set for each room, each contains the connections of the clients in the room.

    def add_roomnick(self, room, nick):
        """Add nick to room. Return generated clientID"""
        # meant to be called from the main handler (page where somebody indicates a nickname and a room to join)
        if len(self.room_info) >= MAX_ROOMS:
            cid = -1
        else:
            if room in self.room_info and len(self.room_info[room]) >= MAX_USERS_PER_ROOM:
                cid = -2
            else:
                roomvalid = re.match(r'[\w-]+$', room)
                nickvalid = re.match(r'[\w-]+$', nick)
                if roomvalid is None:
                    cid = -3
                else:
                    if nickvalid is None:
                        cid = -4
                    else:
                        cid = uuid.uuid4().hex  # generate a client id.
                        if room not in self.room_info:  # it's a new room
                            self.room_info[room] = []
                        c = 1
                        nn = nick
                        nir = self.nicks_in_room(room)
                        while True:
                            if nn in nir:
                                nn = nick + str(c)
                            else:
                                break
                            c += 1
                        self.add_pending(cid, room, nn)
        return cid

    def add_pending(self,cid,room,nick):
        app_log.info("| ADD_PENDING | cid: %s" % cid)
        self.pending_cwsconn[cid] = {'room': room, 'nick': nick}  # still don't know the WS connection for this client

    def remove_pending(self,client_id):
        app_log.info("| REMOVE_PENDING | cid: %s" % client_id)
        if client_id in self.pending_cwsconn:
            del(self.pending_cwsconn[client_id])  # no longer pending

    def add_client_wsconn(self, client_id, conn):
        """Store the websocket connection corresponding to an existing client."""

        # add complete client info to the data structures, remove from the pending dict
        self.client_info[client_id] = self.pending_cwsconn[client_id]
        self.client_info[client_id]['wsconn'] = conn
        room = self.pending_cwsconn[client_id]['room']
        nick = self.pending_cwsconn[client_id]['nick']
        self.room_info[room].append({'cid': client_id, 'nick': nick, 'wsconn': conn})
        self.remove_pending(client_id)
        cid_room = self.client_info[client_id]['room']
        if cid_room in self.roomates:
            self.roomates[cid_room].add(conn)
        else:
            self.roomates[cid_room] = {conn}

        for user in self.room_info[cid_room]:
            if user['cid'] == client_id:
                user['wsconn'] = conn
                break

        # send "join" and and "nick_list" messages
        self.send_join_msg(client_id)
        nick_list = self.nicks_in_room(room)
        cwsconns = self.roomate_cwsconns(client_id)
        self.send_nicks_msg(cwsconns, nick_list)

    def remove_client(self, client_id):
        """Remove all client information from the room handler."""
        cid_room = self.client_info[client_id]['room']
        nick = self.client_info[client_id]['nick']
        # first, remove the client connection from the corresponding room in self.roomates
        client_conn = self.client_info[client_id]['wsconn']
        if client_conn in self.roomates[cid_room]:
            self.roomates[cid_room].remove(client_conn)
            if len(self.roomates[cid_room]) == 0:
                del(self.roomates[cid_room])
        r_cwsconns = self.roomate_cwsconns(client_id)
        # filter out the list of connections r_cwsconns to remove clientID
        r_cwsconns = [conn for conn in r_cwsconns if conn != self.client_info[client_id]['wsconn']]
        self.client_info[client_id] = None
        for user in self.room_info[cid_room]:
            if user['cid'] == client_id:
                self.room_info[cid_room].remove(user)
                break
        self.send_leave_msg(nick, r_cwsconns)
        nick_list = self.nicks_in_room(cid_room)
        self.send_nicks_msg(r_cwsconns, nick_list)
        if len(self.room_info[cid_room]) == 0:  # if room is empty, remove.
            del(self.room_info[cid_room])
            app_log.info("| ROOM_REMOVED | room: %s" % cid_room)

    def nicks_in_room(self, rn):
        """Return a list with the nicknames of the users currently connected to the specified room."""
        nir = []  # nicks in room
        for user in self.room_info[rn]:
            nir.append(user['nick'])
        return nir

    def roomate_cwsconns(self, cid):
        """Return a list with the connections of the users currently connected to the room where
        the specified client (cid) is connected."""
        cid_room = self.client_info[cid]['room']
        r = []
        if cid_room in self.roomates:
            r = self.roomates[cid_room]
        return r

    def send_join_msg(self, client_id):
        """Send a message of type 'join' to all users connected to the room where client_id is connected."""
        nick = self.client_info[client_id]['nick']
        r_cwsconns = self.roomate_cwsconns(client_id)
        msg = {"event": "join", "username": nick, "payload": " joined room " + self.client_info[client_id]['room']}
        pmessage = json.dumps(msg)
        for conn in r_cwsconns:
            conn.write_message(pmessage)

    @staticmethod
    def send_nicks_msg(conns, nick_list):
        """Send a message of type 'nick_list' (contains a list of nicknames) to all the specified connections."""
        msg = {"event": "nick_list", "payload": nick_list}
        pmessage = json.dumps(msg)
        for c in conns:
            c.write_message(pmessage)

    @staticmethod
    def send_leave_msg(nick, rconns):
        """Send a message of type 'leave', specifying the nickname that is leaving, to all the specified connections."""
        msg = {"event": "leave", "username": nick, "payload": " left room"}
        pmessage = json.dumps(msg)
        for conn in rconns:
            conn.write_message(pmessage)


class MainHandler(tornado.web.RequestHandler):

    def initialize(self, room_handler):
        """Store a reference to the "external" RoomHandler instance"""
        self.__rh = room_handler

    def get(self, action=None):
        """Render chat.html if required arguments are present, render main.html otherwise."""
        callback = self.get_argument('callback')
        if not action:  # init startup sequence, won't be completed until the websocket connection is established.
            try:
                room = self.get_argument("room")
                nick = self.get_argument("nick")
                cid = self.__rh.add_roomnick(room, nick)  # this already calls add_pending
                emsgs = ["The nickname provided was invalid. It can only contain letters, numbers, - and _.Please try again.",
                         "The room name provided was invalid. It can only contain letters, numbers, - and _.Please try again.",
                         "The maximum number of users in this room (%d) has been reached. Please try again later."  % MAX_USERS_PER_ROOM,
                         "The maximum number of rooms (%d) has been reached. Please try again later." % MAX_ROOMS]
                if cid == -1 or cid == -2:
                    obj = {
                        'result': 'MaxReached',
                        'emsg': emsgs[cid]
                    }
                    jsonp = "{jsfunc}({json});".format(jsfunc=callback, json=json_encode(obj))
                    self.set_header('Content-Type', 'application/javascript')
                    self.write(jsonp)
                    # self.render("templates/maxreached.html", emsg=emsgs[cid])
                else:
                    if cid == -3 or cid == -4:
                        obj = {
                            'result': 'InvalidName',
                            'emsg': emsgs[cid]
                        }
                        jsonp = "{jsfunc}({json});".format(jsfunc=callback, json=json_encode(obj))
                        self.set_header('Content-Type', 'application/javascript')
                        self.write(jsonp)
                        # self.render("templates/main.html", emsg=emsgs[cid])
                    else:
                        # self.set_cookie("picochess_remote", cid)
                        obj = {
                            'result': 'OK',
                            'room_name': room,
                            'client_id': cid
                        }
                        jsonp = "{jsfunc}({json});".format(jsfunc=callback, json=json_encode(obj))
                        self.set_header('Content-Type', 'application/javascript')
                        self.write(jsonp)
                        # self.render("templates/chat.html", room_name=room)
            except tornado.web.MissingArgumentError:
                obj = {
                    'result': 'MissingArgument',
                }
                jsonp = "{jsfunc}({json});".format(jsfunc=callback, json=json_encode(obj))
                self.set_header('Content-Type', 'application/javascript')
                self.write(jsonp)
                # self.render("templates/main.html", emsg="")
        else:
            if action == "drop":  # drop client from "pending" list. Client cannot establish WS connection.
                client_id = self.get_cookie("picochess_remote")
                if client_id:
                    self.__rh.remove_pending(client_id)
                    obj = {
                        'result': 'DropPending'
                    }
                    jsonp = "{jsfunc}({json});".format(jsfunc=callback, json=json_encode(obj))
                    self.set_header('Content-Type', 'application/javascript')
                    self.write(jsonp)
                    # self.render("templates/nows.html")


class ClientWSConnection(websocket.WebSocketHandler):

    def initialize(self, room_handler):
        """Store a reference to the "external" RoomHandler instance"""
        self.__rh = room_handler

    def check_origin(self, origin):
        return True

    # def open(self):
    #    self.__clientID = self.get_cookie("picochess_remote")
    def open(self, client_id):
        self.__clientID = client_id
        if self.__clientID:
            self.__rh.add_client_wsconn(self.__clientID, self)
            app_log.info("| WS_OPENED | cid: %s" % self.__clientID)
        else:
            app_log.info("cookie cant be read")

    def on_message(self, message):
        msg = json.loads(message)
        mlen = len(msg['payload']) if hasattr(msg, 'payload') else 0
        msg['username'] = self.__rh.client_info[self.__clientID]['nick']
        pmessage = json.dumps(msg)
        rconns = self.__rh.roomate_cwsconns(self.__clientID)
        app_log.info("| MSG_RECEIVED | cid: %s | len: %d" % (self.__clientID, mlen))
        frame = self.make_frame(pmessage)
        for conn in rconns:
            # conn.write_message(pmessage)
            conn.write_frame(frame)

    def make_frame(self, message):
        opcode = 0x1  # we know that binary is false, so opcode is s1
        message = tornado.escape.utf8(message)
        assert isinstance(message, bytes_type)
        finbit = 0x80
        mask_bit = 0
        frame = struct.pack("B", finbit | opcode)
        l = len(message)
        if l < 126:
            frame += struct.pack("B", l | mask_bit)
        elif l <= 0xFFFF:
            frame += struct.pack("!BH", 126 | mask_bit, l)
        else:
            frame += struct.pack("!BQ", 127 | mask_bit, l)
        frame += message
        return frame

    def write_frame(self, frame):
        try:
            # self._write_frame(True, opcode, message)
            self.stream.write(frame)
        except StreamClosedError:
            pass
            # self._abort()

    def on_close(self):
        cid = self.__clientID
        if cid:
            self.__rh.remove_client(cid)
            app_log.info("| WS_CLOSED | cid: %s" % cid)

    def allow_draft76(self):
        return True

if __name__ == "__main__":

    tornado.options.define("port", default="5432", help="Port to listen on.")
    tornado.options.define("ip", default="", help="IP Address to listen on.")
    tornado.options.parse_command_line()

    rh = RoomHandler()
    enable_pretty_logging()
    app_log.level = logging.INFO
    app = tornado.web.Application([
        (r"/(|drop)", MainHandler, {'room_handler': rh}),
        (r"/ws/(.*)", ClientWSConnection, {'room_handler': rh})
        ],
        static_path=os.path.join(os.path.dirname(__file__), "static")
    )
    options = tornado.options.options
    app.listen(options.port, options.ip)
    app_log.info('picochess remote chess server started.')
    app_log.info('listening on %s:%s...' % (options.ip, options.port))
    tornado.ioloop.IOLoop.instance().start()
