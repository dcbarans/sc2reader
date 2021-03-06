from datetime import datetime

from sc2reader.parsers import *
from sc2reader.objects import *
from sc2reader.utils import LITTLE_ENDIAN, BIG_ENDIAN
from sc2reader.utils import key_in_bases, timestamp_from_windows_time

#####################################################
# Metaclass used to help enforce the usage contract
#####################################################
class MetaReader(type):
    def __new__(meta, class_name, bases, class_dict):
        if class_name != "Reader": #Parent class is exempt from checks
            assert 'file' in class_dict or key_in_bases('file',bases), \
                "%s must define the name of the file it reads" % class_name

            assert 'reads' in class_dict or key_in_bases('reads',bases), \
                "%s must define the 'boolean reads(self, build)' member" % class_name

            assert 'read' in class_dict or key_in_bases('read',bases), \
                "%s must define the 'void read(self, buffer, replay)' member" % class_name

        return type.__new__(meta, class_name, bases, class_dict)

class Reader(object):
    __metaclass__ = MetaReader
		
#################################################

class ReplayInitDataReader(Reader):
    file = 'replay.initData'

    def reads(self, build):
        return True
        
    def read(self, buffer, replay):
        
        # Game clients
        for p in range(buffer.read_byte()):
            name = buffer.read_string()
            if len(name) > 0:
                replay.player_names.append(name)
            buffer.skip(5) #Always all zeros UNKNOWN
        
        # UNKNOWN
        buffer.skip(5) # Unknown
        buffer.read_chars(4) # Always Dflt
        buffer.skip(15) #Unknown
        sc_account_id = buffer.read_string()
        
        buffer.skip(684) # Fixed Length data for unknown purpose
        
        while( buffer.read_chars(4).lower() == 's2ma' ):
            buffer.skip(2)
            replay.realm = buffer.read_string(2).lower()
            unknown_map_hash = buffer.read_chars(32)
            
#################################################

class AttributeEventsReader(Reader):
    file = 'replay.attributes.events'
    def reads(self, build):
        return build < 17326
        
    def read(self, buffer, replay):
        self.load_header(replay, buffer)
        
        replay.attributes = list()
        for i in range(0, buffer.read_int(LITTLE_ENDIAN)):
            replay.attributes.append(Attribute([
                    buffer.read_int(LITTLE_ENDIAN),                  #Header
                    buffer.read_int(LITTLE_ENDIAN),                  #Attr Id
                    buffer.read_byte(),                              #Player
                    buffer.read_chars(4) #Value
                ]))
            
    def load_header(self, replay, buffer):
        buffer.read_chars(4)

class AttributeEventsReader_17326(AttributeEventsReader):
    def reads(self, build):
        return build >= 17326

    def load_header(self, replay, buffer):
        buffer.read_chars(5)
        
##################################################

class ReplayDetailsReader(Reader):
    file = 'replay.details'

    def reads(self, build):
        return True
    
    def read(self, buffer, replay):
        data = buffer.read_data_struct()

        for pid, pdata in enumerate(data[0]):
            fields = ('name','battlenet','race','color','??','??','handicap','??','team',)
            pdata = dict(zip(fields, [pdata[i] for i in sorted(pdata.keys())]))

            # TODO?: get a map of realm,subregion => region in here
            player = Player(pid+1, pdata['name'], replay)
            player.uid = pdata['battlenet'][4]
            player.subregion = pdata['battlenet'][2]
            player.handicap = pdata['handicap']
            player.realm = replay.realm

            # Some European language, like DE will have races written slightly differently (ie. Terraner).
            # To avoid these differences, only examine the first letter, which seem to be consistent across languages.
            race = pdata['race']
            if race[0] == 'T':
                race = "Terran"
            if race[0] == 'P':
                race = "Protoss"
            if race[0] == 'Z':
                race = "Zerg"
            # Check against non-western localised races
            player.actual_race = LOCALIZED_RACES.get(race, race)

            color = [pdata['color'][i] for i in sorted(pdata['color'].keys())]
            color = dict(zip(('a','r','g','b',), color))
            color_rgb = "%(r)02X%(g)02X%(b)02X" % color
            player.color = COLOR_CODES.get(color_rgb, color_rgb)
            player.color_rgba = color

            player.team = pdata['team']

            # Add player to replay
            replay.players.append(player)
            
        replay.map = data[1]
        replay.file_time = data[5]

        # TODO: This doesn't seem to produce exactly correct results, ie. often off by one
        # second compared to file timestamps reported by Windows.
        # This might be due to wrong value of the magic constant 116444735995904000
        # or rounding errors. Ceiling or Rounding the result didn't produce consistent
        # results either.
        unix_timestamp = timestamp_from_windows_time(replay.file_time)
        replay.date = datetime.fromtimestamp(unix_timestamp)
        replay.utc_date = datetime.utcfromtimestamp(unix_timestamp)

##################################################

class MessageEventsReader(Reader):
    file = 'replay.message.events'

    def reads(self, build):
        return True
    
    def read(self, buffer, replay):
        replay.messages, time = list(), 0

        while(buffer.left != 0):
            time += buffer.read_timestamp()
            player_id = buffer.read_byte() & 0x0F
            flags = buffer.read_byte()
            
            if flags & 0xF0 == 0x80:
                # Pings, TODO: save and use data somewhere
                if flags & 0x0F == 3:
                    x = buffer.read_int(LITTLE_ENDIAN)
                    y = buffer.read_int(LITTLE_ENDIAN)
                # Some sort of header code
                elif flags & 0x0F == 0:
                    buffer.skip(4) # UNKNOWN
                    # XXX why?
                    replay.other_people.add(player_id)
            
            elif flags & 0x80 == 0:
                target = flags & 0x03
                length = buffer.read_byte()
                
                # Flags for additional length in message
                if flags & 0x08:
                    length += 64
                if flags & 0x10:
                    length += 128
                    
                text = buffer.read_chars(length)
                replay.messages.append(Message(time, player_id, target, text))

####################################################

class GameEventsBase(Reader):
    file = 'replay.game.events'
    def reads(self, build): return False
    
    def read(self, buffer, replay):
        replay.events, frames = list(), 0
        
        PARSERS = {
            0x00: self.get_setup_parser,
            0x01: self.get_action_parser,
            0x02: self.get_unknown2_parser,
            0x03: self.get_camera_parser,
            0x04: self.get_unknown4_parser
        }
        
        while not buffer.empty:
            #Save the start so we can trace for debug purposes
            #start = buffer.cursor

            frames += buffer.read_timestamp()
            pid = buffer.shift(5)
            type, code = buffer.shift(3), buffer.read_byte()
            
            parser = PARSERS[type](code)
            
            if parser == None:
                msg = "Unknown event: %s - %s at %s"
                raise TypeError(msg % (hex(type), hex(code), hex(start)))
            
            event = parser(buffer, frames, type, code, pid)
            buffer.align()
            #event.bytes = buffer.read_range(start,buffer.cursor)
            replay.events.append(event)


    def get_setup_parser(self, code):
        if   code in (0x0B,0x0C): return self.parse_join_event
        elif code == 0x05: return self.parse_start_event
        
    def get_action_parser(self, code):
        if   code == 0x09: return self.parse_leave_event
        elif code & 0x0F == 0xB: return self.parse_ability_event
        elif code & 0x0F == 0xC: return self.parse_selection_event
        elif code & 0x0F == 0xD: return self.parse_hotkey_event
        elif code & 0x0F == 0xF: return self.parse_transfer_event
        
    def get_unknown2_parser(self, code):
        if   code == 0x06: return self.parse_0206_event
        elif code == 0x07: return self.parse_0207_event
        elif code == 0x0E: return self.parse_020E_event
    
    def get_camera_parser(self, code):
        if   code == 0x87: return self.parse_camera87_event
        elif code == 0x08: return self.parse_camera08_event
        elif code == 0x18: return self.parse_camera18_event
        elif code & 0x0F == 1: return self.parse_cameraX1_event
        
    def get_unknown4_parser(self, code):
        if   code == 0x16: return self.parse_0416_event
        elif code == 0xC6: return self.parse_04C6_event
        elif code == 0x87: return self.parse_0487_event
        elif code == 0x00: return self.parse_0400_event
        elif code & 0x0F == 0x02: return self.parse_04X2_event
        elif code & 0x0F == 0x0C: return self.parse_04XC_event
        
class GameEventsReader(GameEventsBase,Unknown2Parser,Unknown4Parser,ActionParser,SetupParser,CameraParser):

    def reads(self, build):
        return True
