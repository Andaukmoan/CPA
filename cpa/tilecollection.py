
from .dbconnect import DBConnect
from .properties import Properties
from .singleton import Singleton
from heapq import heappush, heappop
from weakref import WeakValueDictionary
from . import imagetools
import logging
import numpy
import threading
import wx
import javabridge

db = DBConnect()
p = Properties()

houricon = numpy.array([
[0, 0, 0.4, 0.9, 1, 1, 1, 1, 0.9, 0.4, 0, 0],
[0, 0, 0.6, 1, 0.3, 0.4, 0.4, 0.3, 1, 0.6, 0, 0],
[0, 0, 0.6, 0.8, 0, 0, 0, 0, 0.8, 0.6, 0, 0],
[0, 0, 0.5, 1, 0.2, 0, 0, 0.2, 1, 0.5, 0, 0],
[0, 0, 0, 0.8, 0.9, 0, 0, 0.9, 0.8, 0, 0, 0],
[0, 0, 0, 0, 0.7, 0.9, 0.9, 0.7, 0, 0, 0, 0],
[0, 0, 0, 0, 0.7, 0.9, 0.9, 0.7, 0, 0, 0, 0],
[0, 0, 0, 0.8, 0.9, 0, 0, 0.9, 0.8, 0, 0, 0],
[0, 0, 0.5, 1, 0.2, 0, 0, 0.2, 1, 0.5, 0, 0],
[0, 0, 0.6, 0.8, 0, 0, 0, 0, 0.8, 0.6, 0, 0],
[0, 0, 0.6, 1, 0.3, 0.4, 0.4, 0.3, 1, 0.6, 0, 0],
[0, 0, 0.4, 0.9, 1, 1, 1, 1, 0.9, 0.4, 0, 0],
], dtype=float)

def load_lock():
    return TileCollection().load_lock

class List(list):
    pass

class TileCollection(metaclass=Singleton):
    '''
    Main access point for loading tiles through the TileLoader.
    '''
    def __init__(self):
        self.tileData  = WeakValueDictionary()
        self.loadq     = []
        self.cv        = threading.Condition()
        self.load_lock = threading.Lock()
        self.group_priority = 0
        self.load_icon_template = None
        # Gray placeholder for unloaded images
        tile_size = int(p.image_tile_size)
        if tile_size > 13:
            # Draw a loading icon on the blank tile.
            tgt = (tile_size // 2) - 6
            self.load_icon_template = numpy.zeros((tile_size, tile_size))
            self.load_icon_template[tgt:tgt+12, tgt:tgt+12] = houricon
            self.load_icon_template[self.load_icon_template == 0] = 0.1
            self.load_icon_template[0, 0] = 0
            self.imagePlaceholder = [self.load_icon_template] * sum(map(int,p.channels_per_image))
        else:
            self.imagePlaceholder = List([numpy.zeros((tile_size,
                                                       tile_size))+0.1
                                          for i in range(sum(map(int,p.channels_per_image)))])
        self.loader = TileLoader(self, None)

    def GetTileData(self, obKey, notify_window, priority=1):
        return self.GetTiles([obKey], notify_window, priority)[0]

    def GetTiles(self, obKeys, notify_window, priority=1, display_whole_image=False, processStack=False):
        '''
        obKeys: object tiles to fetch
        notify_window: window that will handle TileUpdatedEvent(s)
        priority: priority with which to fetch these tiles (tiles with
            smaller priorities are pushed to the front of the load queue)
            a 3-tuple is used to provide 3 tiers of priority.
        Returns: a list of lists of tile data (in numpy arrays) in the order
            of the obKeys that were passed in.
        '''
        processStack=True
        if notify_window not in self.loader.notify_window:
            self.loader.notify_window.append(notify_window)
        self.group_priority -= 1
        tiles = []
        temp = {} # for weakrefs
        seen = {} # Record priorities associated with specific images.
        with self.cv:
            for order, obKey in enumerate(obKeys):
                if not obKey in self.tileData:
                    if obKey[0] in seen and not processStack:
                        print("processing together")
                        # An item in the queue had the same source image, process them together.
                        # Heapqueue and the inbuilt cache will allow us to only load the source image once.
                        heappush(self.loadq, ((priority, seen[obKey[0]], order), obKey, display_whole_image))
                    else:
                        print("processing separate")
                        heappush(self.loadq, ((priority, self.group_priority, order), obKey, display_whole_image))
                        seen[obKey[0]] = self.group_priority
                        self.group_priority += 1
                    # We used to generate a full size temporary tile the size of the image,
                    # but we might as well use a small one since it'll be replaced quickly.
                    # if display_whole_image == True:
                    #     imagePlaceholder = [numpy.zeros((int(p.image_size),
                    #                                int(p.image_size)))+0.1
                    #                   for i in range(sum(map(int,p.channels_per_image)))]
                    #     temp[order] = List(imagePlaceholder)
                    temp[order] = List(self.imagePlaceholder)
                    self.tileData[obKey] = temp[order]
            tiles = [self.tileData[obKey] for obKey in obKeys]
            self.cv.notify()
        return tiles


# Event generated by the TileLoader thread.
EVT_TILE_UPDATED_ID = wx.NewId()

def EVT_TILE_UPDATED(win, func):
    '''
    Any class that wishes to handle TileUpdatedEvents must call this function
    with itself as the first parameter, and a handler as the second parameter.
    '''
    win.Connect(-1, -1, EVT_TILE_UPDATED_ID, func)


class TileUpdatedEvent(wx.PyEvent):
    '''
    This event type is posted whenever an ImageTile has been updated by the
    TileLoader thread.
    '''
    def __init__(self, data):
        wx.PyEvent.__init__(self)
        self.SetEventType(EVT_TILE_UPDATED_ID)
        self.data = data


class TileLoader(threading.Thread):
    '''
    This thread is owned by the TileCollection singleton and is kept
    running for the duration of the app execution.  Whenever
    TileCollection has obKeys in its load queue (loadq), this thread
    will remove them from the queue and fetch the tile data for
    them. The tile data is then written back into TileCollection's
    tileData dict over the existing placeholder. Finally an event is
    posted to the svn to tell it to refresh the tiles.
    '''
    def __init__(self, tc, notify_window):
        threading.Thread.__init__(self)
        self.setName('TileLoader_%s'%(self.getName()))
        self.notify_window = []
        if notify_window is not None:
            self.notify_window.append(notify_window)
        self.tile_collection = tc
        self._want_abort = False
        self.start()

    def run(self):
        if p.force_bioformats:
            logging.debug("Starting javabridge")
            import bioformats
            javabridge.start_vm(class_path=bioformats.JARS, run_headless=True)
            javabridge.attach()
        try:
            while 1:
                self.tile_collection.cv.acquire()
                # If there are no objects in the queue then wait
                while not self.tile_collection.loadq:
                    self.tile_collection.cv.wait()

                if self._want_abort:
                    self.tile_collection.cv.release()
                    db = DBConnect()
                    db.CloseConnection()
                    logging.info('%s aborted'%self.getName())
                    return

                data = heappop(self.tile_collection.loadq)
                obKey = data[1]
                display_whole_image = data[2] #display whole image instead of object image

                self.tile_collection.cv.release()

                # wait until loading has completed before continuing
                with self.tile_collection.load_lock:
                    # Make sure tile hasn't been deleted outside this thread
                    if not self.tile_collection.tileData.get(obKey, None):
                        continue

                    # Get the tile
                    new_data = imagetools.FetchTile(obKey, display_whole_image=display_whole_image)
                    #new_data=None
                    if new_data is None:
                        #if fetching fails, leave the tile blank
                        continue

                    tile_data = self.tile_collection.tileData.get(obKey, None)

                    # Make sure tile hasn't been deleted outside this thread
                    if tile_data is not None:
                        # copy each channel
                        for i in range(len(tile_data)):
                            tile_data[i] = new_data[i]
                        for window in self.notify_window:
                            wx.PostEvent(window, TileUpdatedEvent(obKey))
        finally:
            if javabridge.get_env() is not None:
                javabridge.detach()

    def abort(self):
        self._want_abort = True
        self.tile_collection.cv.acquire()
        heappush(self.tile_collection.loadq, ((0, 0, 0), '<ABORT>'))
        self.tile_collection.cv.notify()
        self.tile_collection.cv.release()



################# FOR TESTING ##########################
if __name__ == "__main__":
    app = wx.App()


    from .datamodel import DataModel
    p = Properties()
    p.show_load_dialog()
    db = DBConnect()
    db.connect()
    dm = DataModel()

    test = TileCollection()

    f =  wx.Frame(None)
    for i in range(10):
        obKey = dm.GetRandomObject(1)
        test.GetTileData((0,1,1), f)

    for t in threading.enumerate():
        if t != threading.currentThread():
            t.abort()
    f.Destroy()

    app.MainLoop()
