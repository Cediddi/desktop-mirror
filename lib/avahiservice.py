#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import logging.handlers
import platform
import socket
from select import select
from threading import Thread, Timer, Lock
from common import DEFAULT_PORT

# local libraries
import pybonjour as pb

INCLUDE_SELF = False


class AvahiService(Thread):
    TIMEOUT = 5

    def __init__(self, callback):
        Thread.__init__(self, name='AvahiService')
        self._callback = callback
        self._stoped = True
        self._targets = dict()
        self._hosts = dict()
        self._input = []
        self._lock = Lock()
        self._queried = []
        self._resolved = []

    @property
    def targets(self):
        return self._targets

    @property
    def hosts(self):
        return self._hosts

    def query_callback(self, sdRef, flags, interfaceIndex, errorCode,
                       fullname, rrtype, rrclass, rdata, ttl):
        #self.remove_sd(sdRef)
        if errorCode != pb.kDNSServiceErr_NoError:
            logging.error('AvahiService: query_callback: error={}'.
                          format(errorCode))
            return

        self._queried.append(True)
        ip = socket.inet_ntoa(rdata)
        fullname = fullname[:fullname.rfind('.local')]
        logging.debug('Queryed service:: fullname={}, IP={}'.
                      format(fullname, ip))
        if fullname not in self._hosts:
            self._hosts[fullname] = []
        if ip in self._hosts[fullname]:
            return
        self._hosts[fullname].append(ip)

    def resolve_callback(self, sdRef, flags, interfaceIndex, errorCode,
                         fullname, hosttarget, port, txtRecord):
        if errorCode != pb.kDNSServiceErr_NoError:
            logging.error('AvahiService: resolve_callback: error={}'.
                          format(errorCode))
            return

        logging.debug('Resolved service: {}; {}; {}'.format(fullname,
                                                            hosttarget,
                                                            port))
        if fullname not in self._targets:
            self._targets[fullname] = []
        host = hosttarget[:hosttarget.rfind('.local')]
        if INCLUDE_SELF or host != platform.node():
            service = fullname[fullname.find('.') + 1:-7]
            target = {'host': host, 'port': port, 'service': service}
            self._targets[fullname].append(target)
        self.fire_event()

        self._lock.acquire()
        sd = pb.DNSServiceQueryRecord(interfaceIndex=interfaceIndex,
                                      fullname=hosttarget,
                                      rrtype=pb.kDNSServiceType_A,
                                      callBack=self.query_callback)
        old_input = self._input
        self._input = (sd,)
        self._lock.release()

        while not self._stoped and not self._queried:
            R, W, E = select(self._input, [], [], self.TIMEOUT)
            if not (R or W or E):
                break
            map(lambda sd: pb.DNSServiceProcessResult(sd), R)
        else:
            self._queried.pop()

        self.remove_input()
        self._lock.acquire()
        self._input = old_input
        self._lock.release()

        self._resolved.append(True)

    def removed_callback(self, fullname):
        if fullname in self._targets:
            self.fire_event()
            del self._targets[fullname]
        if fullname in self._hosts:
            del self._hosts[fullname]
        logging.info('Service removed: {}'.format(fullname))

    def browse_callback(self, sdRef, flags, interfaceIndex, errorCode,
                        serviceName, regtype, replyDomain):
        if errorCode != pb.kDNSServiceErr_NoError:
            logging.error('AvahiService: browse_callback: error={}'.
                          format(errorCode))
            return

        fullname = '{}.{}{}'.format(serviceName, regtype, replyDomain)
        if not (flags & pb.kDNSServiceFlagsAdd):
            self.removed_callback(fullname)
            return

        logging.debug('Service added {}; resolving'.format(fullname))
        self._lock.acquire()
        old_input = self._input
        sd = pb.DNSServiceResolve(0,
                                  interfaceIndex,
                                  serviceName,
                                  regtype,
                                  replyDomain,
                                  self.resolve_callback)
        self._input = (sd,)
        self._lock.release()

        while not self._stoped and not self._resolved:
            R, W, E = select(self._input, [], [], self.TIMEOUT)
            if not (R or W or E):
                break
            map(lambda sd: pb.DNSServiceProcessResult(sd), R)
        else:
            self._resolved.pop()

        self.remove_input()
        self._lock.acquire()
        self._input = old_input
        self._lock.release()

    def run(self):
        self._stoped = False
        logging.info('AvahiService started')

        self.register_service(platform.node(), '_desktop-mirror._tcp',
                              DEFAULT_PORT)
        self.listen_browse(('_xbmc-web._tcp', '_desktop-mirror._tcp'),
                           self.browse_callback)
        logging.info('AvahiService stoped')

        self._stoped = True

    def register_service(self, name, regtype, port):
        def register_callback(sdRef, flags, errorCode, name, regtype,
                              domain):
            if errorCode == pb.kDNSServiceErr_NoError:
                logging.info('Registered service:')
                logging.info('  name    = ' + name)
                logging.info('  regtype = ' + regtype)
                logging.info('  domain  = ' + domain)
            else:
                logging.error('Failed to register service: {}'.
                              format(errorCode))
            #logging.debug('remove sd in sub: ' + str(sdRef))
            self._done = True

        self._done = False
        self._lock.acquire()
        sd = pb.DNSServiceRegister(name=name,
                                   regtype=regtype,
                                   port=port,
                                   callBack=register_callback)
        self._input.append(sd)
        self._lock.release()

        while not self._stoped and not self._done:
            R, W, E = select(self._input, [], [], self.TIMEOUT)
            if not (R or W or E):
                # timeout
                continue
            for sd in R:
                pb.DNSServiceProcessResult(sd)

        self._lock.acquire()
        self._input = []
        self._lock.release()

    def listen_browse(self, services, cb):
        for regtype in services:
            sd = pb.DNSServiceBrowse(regtype=regtype,
                                     callBack=cb)
            self._input.append(sd)

        try:
            while not self._stoped:
                R, W, E = select(self._input, [], [], self.TIMEOUT)
                if not (R or W or E):
                    continue
                for sd in R:
                    pb.DNSServiceProcessResult(sd)
        except Exception as e:
            logging.warn(str(e))
        finally:
            self.remove_input()

    def stop(self):
        self._resolved.append(True)
        self._queried.append(True)
        self._stoped = True
        if hasattr(self, '_fire_timer') and self._fire_timer is not None:
            self._fire_timer.cancel()
        self.remove_input(True)
        self.join(3)

    def fire_event(self):
        def callback():
            try:
                self._callback(None)
            except:
                pass
        if hasattr(self, '_fire_timer') and self._fire_timer is not None:
            self._fire_timer.cancel()
        self._fire_timer = Timer(1.0, lambda: callback())
        self._fire_timer.start()

    def remove_input(self, force=False):
        if not force:
            self._lock.acquire()
        try:
            for sd in self._input:
                sd.close()
            self._input = []
        except:
            import traceback
            import sys
            exc_type, exc_value, exc_traceback = sys.exc_info()
            lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
            logging.warn(''.join('!! ' + line for line in lines))
        if not force:
            self._lock.release()
