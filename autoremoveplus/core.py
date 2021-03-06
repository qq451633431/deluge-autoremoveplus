#
# core.py
#
# Copyright (C) 2014 Omar Alvarez <osurfer3@hotmail.com>
# Copyright (C) 2011 Jamie Lennox <jamielennox@gmail.com>
#
# Basic plugin template created by:
# Copyright (C) 2008 Martijn Voncken <mvoncken@gmail.com>
# Copyright (C) 2007-2009 Andrew Resch <andrewresch@gmail.com>
# Copyright (C) 2009 Damien Churchill <damoxc@gmail.com>
#
# Deluge is free software.
#
# You may redistribute it and/or modify it under the terms of the
# GNU General Public License, as published by the Free Software
# Foundation; either version 3 of the License, or (at your option)
# any later version.
#
# deluge is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with deluge.    If not, write to:
# 	The Free Software Foundation, Inc.,
# 	51 Franklin Street, Fifth Floor
# 	Boston, MA  02110-1301, USA.
#
#    In addition, as a special exception, the copyright holders give
#    permission to link the code of portions of this program with the OpenSSL
#    library.
#    You must obey the GNU General Public License in all respects for all of
#    the code used other than OpenSSL. If you modify file(s) with this
#    exception, you may extend this exception to your version of the file(s),
#    but you are not obligated to do so. If you do not wish to do so, delete
#    this exception statement from your version. If you delete this exception
#    statement from all source files in the program, then also delete it here.
#

from deluge.log import LOG as log
from deluge.plugins.pluginbase import CorePluginBase
import deluge.component as component
import deluge.configmanager
from deluge.core.rpcserver import export

from twisted.internet import reactor
from twisted.internet.task import LoopingCall, deferLater

from urlparse import urlparse
import time

DEFAULT_PREFS = {
    'max_seeds' : -1,
    'filter' : 'func_ratio',
    'count_exempt' : False,
    'remove_data' : False,
    'trackers' : [],
    'min' : 0.0,
    'interval' : 0.5,
    'sel_func' : 'and',
    'filter2' : 'func_added',
    'min2' : 0.0
}

def _get_ratio((i, t)): 
    return t.get_ratio()

def _date_added((i, t)): 
    #return -t.time_added 
    return (time.time()-t.time_added)/86400.0

filter_funcs = { 
    'func_ratio' : _get_ratio, 
    'func_added' : lambda (i, t): (time.time()-t.time_added)/86400.0,
    'func_seed_time' : lambda (i, t): t.get_status(['seeding_time'])['seeding_time']/86400.0
}

sel_funcs = {
    'and': lambda (a, b): a and b,
    'or' : lambda (a, b): a or b
}

live = True

class Core(CorePluginBase):

    def enable(self):
        log.debug ("AutoRemovePlus: Enabled")
        self.config = deluge.configmanager.ConfigManager("autoremoveplus.conf", DEFAULT_PREFS)
        self.torrent_states = deluge.configmanager.ConfigManager("autoremoveplusstates.conf", {})
        
        # Safe after loading to have a default configuration if no gtkui is available
        self.config.save()
        self.torrent_states.save()
        
        #eventmanager = component.get("EventManager")
        #eventmanager.register_event_handler("TorrentFinishedEvent", self.do_remove)

        # it appears that if the plugin is enabled on boot then it is called before the 
        # torrents are properly loaded and so do_remove receives an empty list. So we must 
        # listen to SessionStarted for when deluge boots but we still have apply_now so that 
        # if the plugin is enabled mid-program do_remove is still run
        #eventmanager.register_event_handler("SessionStartedEvent", self.do_remove)  
        self.looping_call = LoopingCall(self.do_remove)
        deferLater(reactor, 5, self.start_looping)     

    def disable(self):
        #eventmanager = component.get("EventManager")
        #eventmanager.deregister_event_handler("TorrentFinishedEvent", self.do_remove)
        #eventmanager.deregister_event_handler("SessionStartedEvent", self.do_remove)
        if self.looping_call.running:
            self.looping_call.stop()

    def update(self):
        # why does update only seem to get called when the plugin is enabled in this session ??
        pass

    def start_looping(self):
        log.warning('check interval loop starting')
        self.looping_call.start(self.config['interval'] * 86400.0)

    @export
    def set_config(self, config):
        """Sets the config dictionary"""
        for key in config.keys():
            self.config[key] = config[key]
        self.config.save()
        if self.looping_call.running:
            self.looping_call.stop()
        self.looping_call.start(self.config['interval'] * 86400.0)

    @export
    def get_config(self):
        """Returns the config dictionary"""
        return self.config.config

    @export 
    def get_remove_rules(self): 
        return {
            'func_ratio' : 'Ratio',  
            'func_added' : 'Date Added',
            'func_seed_time' : 'Seed Time'
        }

    @export
    def get_ignore(self, torrent_ids): 
        if not hasattr(torrent_ids, '__iter__'): 
            torrent_ids = [torrent_ids] 

        return [ self.torrent_states.config.get(t, False) for t in torrent_ids ] 

    @export 
    def set_ignore(self, torrent_ids, ignore = True): 
        log.debug ("AutoRemovePlus: Setting torrents %s to ignore=%s" % (torrent_ids, ignore))

        if not hasattr(torrent_ids, '__iter__'): 
            torrent_ids = [torrent_ids] 

        for t in torrent_ids: 
            self.torrent_states[t] = ignore 

        self.torrent_states.save()

    # we don't use args or kwargs it just allows callbacks to happen cleanly
    def do_remove(self, *args, **kwargs): 
        log.debug("AutoRemovePlus: do_remove")

        max_seeds = self.config['max_seeds'] 
        count_exempt = self.config['count_exempt']
        remove_data = self.config['remove_data']
        exemp_trackers = self.config['trackers']
        min_val = self.config['min']
        min_val2 = self.config['min2']

        # Negative max means unlimited seeds are allowed, so don't do anything
        if max_seeds < 0: 
            return 

        torrentmanager = component.get("TorrentManager")
        torrent_ids = torrentmanager.get_torrent_list()

        log.debug("Number of torrents: {0}".format(len(torrent_ids)))
                  
        # If there are less torrents present than we allow then there can be nothing to do 
        if len(torrent_ids) <= max_seeds: 
            return 
        
        torrents = []
        ignored_torrents = []

        # relevant torrents to us exist and are finished 
        for i in torrent_ids: 
            t = torrentmanager.torrents.get(i, None)

            #log.debug("Time added: %f" % (t.time_added))
            #log.debug("Ratio: %f" % (t.get_ratio()))
            #log.debug("Seed time: %f" % (t.get_status(['seeding_time'])['seeding_time']))

            try:
                finished = t.is_finished
            except: 
                continue
            else: 
                if not finished: 
                    continue

            #if not (t.state == "Seeding"):
            #    continue

            try: 
                ignored = self.torrent_states[i]
            except KeyError:
                ignored = False

            ex_torrent = False
            trackers = t.trackers
            # for tracker in trackers:
            #     log.debug("%s" % (urlparse(tracker['url'].replace("udp://","http://")).hostname))
            #     for ex_tracker in exemp_trackers:
            #         if(tracker['url'].find(ex_tracker.lower()) != -1):
            #             log.debug("Found exempted tracker: %s" % (ex_tracker))
            #             ex_torrent = True
            for tracker,ex_tracker in ((t,ex_t) for t in trackers for ex_t in exemp_trackers):
                if(tracker['url'].find(ex_tracker.lower()) != -1):
                    log.debug("Found exempted tracker: %s" % (ex_tracker))
                    ex_torrent = True

            (ignored_torrents if ignored or ex_torrent else torrents).append((i, t))

        log.debug("Number of finished torrents: {0}".format(len(torrents)))
        log.debug("Number of ignored torrents: {0}".format(len(ignored_torrents)))
        

        # now that we have trimmed active torrents check again to make sure we still need to proceed
        if len(torrents) + (len(ignored_torrents) if count_exempt else 0) <= max_seeds: 
            return 

        # if we are counting ignored torrents towards our maximum then these have to come off the top of our allowance
        if count_exempt: 
            max_seeds -= len(ignored_torrents)
            if max_seeds < 0: max_seeds = 0 
        
        #Sort it according to our chosen method 
        #By only one key
        #torrents.sort(key = filter_funcs.get(self.config['filter'], _get_ratio), reverse = False)

        #By primary criteria and secondary
        #torrents.sort(key = filter_funcs.get(self.config['filter2'], _get_ratio), reverse = False)
        #torrents.sort(key = filter_funcs.get(self.config['filter'], _get_ratio), reverse = False)

        #Alternate sort by primary criteria and secondary
        torrents.sort(key = lambda x : (filter_funcs.get(self.config['filter'], _get_ratio)(x), filter_funcs.get(self.config['filter2'], _get_ratio)(x)), reverse = False)

        changed = False
        # remove these torrents
        for i, t in torrents[max_seeds:]: 
            log.debug("AutoRemovePlus: Remove torrent %s, %s" % (i, t.get_status(['name'])['name']))
            log.debug(filter_funcs.get(self.config['filter'], _get_ratio)((i,t)))
            log.debug(filter_funcs.get(self.config['filter2'], _get_ratio)((i,t)))
            if live: 
                if sel_funcs.get(self.config['sel_func'])((filter_funcs.get(self.config['filter'], _get_ratio)((i,t)) >= min_val, filter_funcs.get(self.config['filter2'], _get_ratio)((i,t)) >= min_val2)):
                    try:
                        torrentmanager.remove(i, remove_data = remove_data)
                    except Exception, e: 
                        log.warn("AutoRemovePlus: Problems removing torrent: %s", e)

                    try: 
                        del self.torrent_states.config[i] 
                    except KeyError: 
                        pass
                    else: 
                        changed = True
        if changed: 
            self.torrent_states.save()
         
