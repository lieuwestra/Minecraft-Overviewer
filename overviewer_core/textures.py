#    This file is part of the Minecraft Overviewer.
#
#    Minecraft Overviewer is free software: you can redistribute it and/or
#    modify it under the terms of the GNU General Public License as published
#    by the Free Software Foundation, either version 3 of the License, or (at
#    your option) any later version.
#
#    Minecraft Overviewer is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
#    Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with the Overviewer.  If not, see <http://www.gnu.org/licenses/>.

from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
import re
import sys
import imp
import json
import os
import os.path
import zipfile
from io import BytesIO
import math
from random import randint
import numpy
from PIL import Image, ImageEnhance, ImageOps, ImageDraw
import logging
import functools

from . import util

# global variables to collate information in @material decorators
blockmap_generators = {}
block_models = {}

known_blocks = set()
used_datas = set()
max_blockid = 0
max_data = 0
next_unclaimed_id = 2048

transparent_blocks = set()
solid_blocks = set()
fluid_blocks = set()
nospawn_blocks = set()


# This is here for circular import reasons.
# Please don't ask, I choose to repress these memories.
# ... okay fine I'll tell you.
# Initialising the C extension requires access to the globals above.
# Due to the circular import, this wouldn't work, unless we reload the
# module in the C extension or just move the import below its dependencies.
from .c_overviewer import alpha_over


class TextureException(Exception):
    "To be thrown when a texture is not found."
    pass


color_map = ["white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
             "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black"]


##
## Textures object
##
class Textures(object):
    """An object that generates a set of block sprites to use while
    rendering. It accepts a background color, north direction, and
    local textures path.
    """

    def __init__(self, texturepath=None, bgcolor=(26, 26, 26, 0), northdirection=0):
        self.bgcolor = bgcolor
        self.rotation = northdirection
        self.find_file_local_path = texturepath
        
        # not yet configurable
        self.texture_size = 24
        self.texture_dimensions = (self.texture_size, self.texture_size)
        
        # this is set in in generate()
        self.generated = False

        # see load_image_texture()
        self.texture_cache = {}

        # once we find a jarfile that contains a texture, we cache the ZipFile object here
        self.jars = OrderedDict()
    
    ##
    ## pickle support
    ##
    
    def __getstate__(self):
        # we must get rid of the huge image lists, and other images
        attributes = self.__dict__.copy()
        for attr in ['blockmap', 'biome_grass_texture', 'watertexture', 'lavatexture', 'firetexture', 'portaltexture', 'lightcolor', 'grasscolor', 'foliagecolor', 'watercolor', 'texture_cache']:
            try:
                del attributes[attr]
            except KeyError:
                pass
        attributes['jars'] = OrderedDict()
        return attributes

    def __setstate__(self, attrs):
        # regenerate textures, if needed
        for attr, val in list(attrs.items()):
            setattr(self, attr, val)
        self.texture_cache = {}
        if self.generated:
            self.generate()
    
    ##
    ## The big one: generate()
    ##
    
    def generate(self):
        # Make sure we have the foliage/grasscolor images available
        try:
            self.load_foliage_color()
            self.load_grass_color()
        except TextureException as e:
            logging.error(
                "Your system is missing either assets/minecraft/textures/colormap/foliage.png "
                "or assets/minecraft/textures/colormap/grass.png. Either complement your "
                "resource pack with these texture files, or install the vanilla Minecraft "
                "client to use as a fallback.")
            raise e
        
        # generate biome grass mask
        self.biome_grass_texture = self.build_block(self.load_image_texture("assets/minecraft/textures/block/grass_block_top.png"), self.load_image_texture("assets/minecraft/textures/block/grass_block_side_overlay.png"))
        
        # generate the blocks
        global blockmap_generators
        self.blockmap = [None] * max_blockid * max_data

        for (blockid, data), texgen in list(blockmap_generators.items()):
            tex = texgen(self, blockid, data)
            self.blockmap[blockid * max_data + data] = self.generate_texture_tuple(tex)
        
        if self.texture_size != 24:
            # rescale biome grass
            self.biome_grass_texture = self.biome_grass_texture.resize(self.texture_dimensions, Image.ANTIALIAS)
            
            # rescale the rest
            for i, tex in enumerate(self.blockmap):
                if tex is None:
                    continue
                block = tex[0]
                scaled_block = block.resize(self.texture_dimensions, Image.ANTIALIAS)
                self.blockmap[i] = self.generate_texture_tuple(scaled_block)
        
        self.generated = True
    
    # TODO: load models from resource packs, for now only client jars are used
    # TODO: load blockstate before models
    def find_models(self, verbose=False):
        filename = 'assets/minecraft/models/'
        versiondir = self.versiondir(verbose)
        available_versions = self.available_versions(versiondir, verbose)
        
        if not available_versions:
            if verbose:
                logging.info("Did not find any non-snapshot minecraft jars >=1.8.0")
        while(available_versions):
            most_recent_version = available_versions.pop(0)
            if verbose:
                logging.info("Trying {0}. Searching it for the file...".format(
                    ".".join(str(x) for x in most_recent_version)))

            jarname = ".".join(str(x) for x in most_recent_version)
            jarpath = os.path.join(versiondir, jarname, jarname + ".jar")

            jar = {}

            if jarpath in self.jars:
                jar = self.jars[jarpath]
            elif os.path.isfile(jarpath):
                try:
                    jar = zipfile.ZipFile(jarpath)
                except (KeyError, IOError) as e:
                    pass
                except (zipfile.BadZipFile) as e:
                    logging.warning("Your jar {0} is corrupted, I'll be skipping it, but you "
                                    "should probably look into that.".format(jarpath))
            else:
                if verbose:
                    logging.info("Did not find file {0} in jar {1}".format(filename, jarpath))
                continue

            models = []
            for file in jar.namelist():
                if file.startswith('assets/minecraft/models/block'):
                    model = Path(file).stem
                    models.append(model)

            return models
            
    ##
    ## Helpers for opening textures
    ##
    
    def find_file(self, filename, mode="rb", verbose=False):
        """Searches for the given file and returns an open handle to it.
        This searches the following locations in this order:
        
        * In the directory textures_path given in the initializer if not already open
        * In an already open resource pack or client jar file
        * In the resource pack given by textures_path
        * The program dir (same dir as overviewer.py) for extracted textures
        * On Darwin, in /Applications/Minecraft for extracted textures
        * Inside a minecraft client jar. Client jars are searched for in the
          following location depending on platform:
        
            * On Windows, at %APPDATA%/.minecraft/versions/
            * On Darwin, at
                $HOME/Library/Application Support/minecraft/versions
            * at $HOME/.minecraft/versions/

          Only the latest non-snapshot version >1.6 is used

        * The overviewer_core/data/textures dir
        
        """
        if verbose: logging.info("Starting search for {0}".format(filename))

        # Look for the file is stored in with the overviewer
        # installation. We include a few files that aren't included with Minecraft
        # textures. This used to be for things such as water and lava, since
        # they were generated by the game and not stored as images. Nowdays I
        # believe that's not true, but we still have a few files distributed
        # with overviewer.
        # Do this first so we don't try all .jar files for stuff like "water.png"
        programdir = util.get_program_path()
        if verbose: logging.info("Looking for texture in overviewer_core/data/textures")
        path = os.path.join(programdir, "overviewer_core", "data", "textures", filename)
        if os.path.isfile(path):
            if verbose: logging.info("Found %s in '%s'", filename, path)
            return open(path, mode)
        elif hasattr(sys, "frozen") or imp.is_frozen("__main__"):
            # windows special case, when the package dir doesn't exist
            path = os.path.join(programdir, "textures", filename)
            if os.path.isfile(path):
                if verbose: logging.info("Found %s in '%s'", filename, path)
                return open(path, mode)

        # A texture path was given on the command line. Search this location
        # for the file first.
        if self.find_file_local_path:
            if (self.find_file_local_path not in self.jars
                and os.path.isfile(self.find_file_local_path)):
                # Must be a resource pack. Look for the requested file within
                # it.
                try:
                    pack = zipfile.ZipFile(self.find_file_local_path)
                    # pack.getinfo() will raise KeyError if the file is
                    # not found.
                    pack.getinfo(filename)
                    if verbose: logging.info("Found %s in '%s'", filename,
                                             self.find_file_local_path)
                    self.jars[self.find_file_local_path] = pack
                    # ok cool now move this to the start so we pick it first
                    self.jars.move_to_end(self.find_file_local_path, last=False)
                    return pack.open(filename)
                except (zipfile.BadZipfile, KeyError, IOError):
                    pass
            elif os.path.isdir(self.find_file_local_path):
                full_path = os.path.join(self.find_file_local_path, filename)
                if os.path.isfile(full_path):
                        if verbose: logging.info("Found %s in '%s'", filename, full_path)
                        return open(full_path, mode)

        # We already have some jars open, better use them.
        if len(self.jars) > 0:
            for jarpath in self.jars:
                try:
                    jar = self.jars[jarpath]
                    jar.getinfo(filename)
                    if verbose: logging.info("Found (cached) %s in '%s'", filename,
                                             jarpath)
                    return jar.open(filename)
                except (KeyError, IOError) as e:
                    pass

        # If we haven't returned at this point, then the requested file was NOT
        # found in the user-specified texture path or resource pack.
        if verbose: logging.info("Did not find the file in specified texture path")


        # Look in the location of the overviewer executable for the given path
        path = os.path.join(programdir, filename)
        if os.path.isfile(path):
            if verbose: logging.info("Found %s in '%s'", filename, path)
            return open(path, mode)

        if sys.platform.startswith("darwin"):
            path = os.path.join("/Applications/Minecraft", filename)
            if os.path.isfile(path):
                if verbose: logging.info("Found %s in '%s'", filename, path)
                return open(path, mode)

        if verbose: logging.info("Did not find the file in overviewer executable directory")
        if verbose: logging.info("Looking for installed minecraft jar files...")

        # Find an installed minecraft client jar and look in it for the texture
        # file we need.
        versiondir = self.versiondir(verbose)
        available_versions = self.available_versions(versiondir, verbose)
        
        if not available_versions:
            if verbose: logging.info("Did not find any non-snapshot minecraft jars >=1.8.0")
        while(available_versions):
            most_recent_version = available_versions.pop(0)
            if verbose: logging.info("Trying {0}. Searching it for the file...".format(".".join(str(x) for x in most_recent_version)))

            jarname = ".".join(str(x) for x in most_recent_version)
            jarpath = os.path.join(versiondir, jarname, jarname + ".jar")

            if os.path.isfile(jarpath):
                try:
                    jar = zipfile.ZipFile(jarpath)
                    jar.getinfo(filename)
                    if verbose: logging.info("Found %s in '%s'", filename, jarpath)
                    self.jars[jarpath] = jar
                    return jar.open(filename)
                except (KeyError, IOError) as e:
                    pass
                except (zipfile.BadZipFile) as e:
                    logging.warning("Your jar {0} is corrupted, I'll be skipping it, but you "
                                    "should probably look into that.".format(jarpath))

            if verbose: logging.info("Did not find file {0} in jar {1}".format(filename, jarpath))
            

        raise TextureException("Could not find the textures while searching for '{0}'. Try specifying the 'texturepath' option in your config file.\nSet it to the path to a Minecraft Resource pack.\nAlternately, install the Minecraft client (which includes textures)\nAlso see <http://docs.overviewer.org/en/latest/running/#installing-the-textures>\n(Remember, this version of Overviewer requires a 1.19-compatible resource pack)\n(Also note that I won't automatically use snapshots; you'll have to use the texturepath option to use a snapshot jar)".format(filename))

    def versiondir(self, verbose):
        versiondir = ""
        if "APPDATA" in os.environ and sys.platform.startswith("win"):
            versiondir = os.path.join(os.environ['APPDATA'], ".minecraft", "versions")
        elif "HOME" in os.environ:
            # For linux:
            versiondir = os.path.join(os.environ['HOME'], ".minecraft", "versions")
            if not os.path.exists(versiondir) and sys.platform.startswith("darwin"):
                # For Mac:
                versiondir = os.path.join(os.environ['HOME'], "Library",
                    "Application Support", "minecraft", "versions")
        return versiondir

    def available_versions(self, versiondir, verbose):
        try:
            if verbose: logging.info("Looking in the following directory: \"%s\"" % versiondir)
            versions = os.listdir(versiondir)
            if verbose: logging.info("Found these versions: {0}".format(versions))
        except OSError:
            # Directory doesn't exist? Ignore it. It will find no versions and
            # fall through the checks below to the error at the bottom of the
            # method.
            versions = []

        available_versions = []
        for version in versions:
            # Look for the latest non-snapshot that is at least 1.8. This
            # version is only compatible with >=1.8, and we cannot in general
            # tell if a snapshot is more or less recent than a release.

            # Allow two component names such as "1.8" and three component names
            # such as "1.8.1"
            if version.count(".") not in (1,2):
                continue
            try:
                versionparts = [int(x) for x in version.split(".")]
            except ValueError:
                continue

            if versionparts < [1,8]:
                continue

            available_versions.append(versionparts)

        available_versions.sort(reverse=True)

        return available_versions

    def load_image_texture(self, filename):
        # Textures may be animated or in a different resolution than 16x16.  
        # This method will always return a 16x16 image

        img = self.load_image(filename)

        w,h = img.size
        if w != h:
            img = img.crop((0,0,w,w))
        if w != 16:
            img = img.resize((16, 16), Image.ANTIALIAS)

        self.texture_cache[filename] = img
        return img

    def load_image(self, filename):
        """Returns an image object"""

        try:
            img = self.texture_cache[filename]
            if isinstance(img, Exception):  # Did we cache an exception?
                raise img                   # Okay then, raise it.
            return img
        except KeyError:
            pass
        
        try:
            fileobj = self.find_file(filename, verbose=logging.getLogger().isEnabledFor(logging.DEBUG))
        except (TextureException, IOError) as e:
            # We cache when our good friend find_file can't find
            # a texture, so that we do not repeatedly search for it.
            self.texture_cache[filename] = e
            raise e
        buffer = BytesIO(fileobj.read())
        try:
            img = Image.open(buffer).convert("RGBA")
        except IOError:
            raise TextureException("The texture {} appears to be corrupted. Please fix it. Run "
                                   "Overviewer in verbose mode (-v) to find out where I loaded "
                                   "that file from.".format(filename))
        self.texture_cache[filename] = img
        return img

    def load_water(self):
        """Special-case function for loading water."""
        watertexture = getattr(self, "watertexture", None)
        if watertexture:
            return watertexture
        watertexture = self.load_image_texture("assets/minecraft/textures/block/water_still.png")
        self.watertexture = watertexture
        return watertexture

    def load_lava(self):
        """Special-case function for loading lava."""
        lavatexture = getattr(self, "lavatexture", None)
        if lavatexture:
            return lavatexture
        lavatexture = self.load_image_texture("assets/minecraft/textures/block/lava_still.png")
        self.lavatexture = lavatexture
        return lavatexture
    
    def load_portal(self):
        """Special-case function for loading portal."""
        portaltexture = getattr(self, "portaltexture", None)
        if portaltexture:
            return portaltexture
        portaltexture = self.load_image_texture("assets/minecraft/textures/block/nether_portal.png")
        self.portaltexture = portaltexture
        return portaltexture
    
    def load_light_color(self):
        """Helper function to load the light color texture."""
        if hasattr(self, "lightcolor"):
            return self.lightcolor
        try:
            lightcolor = list(self.load_image("light_normal.png").getdata())
        except Exception:
            logging.warning("Light color image could not be found.")
            lightcolor = None
        self.lightcolor = lightcolor
        return lightcolor
    
    def load_grass_color(self):
        """Helper function to load the grass color texture."""
        if not hasattr(self, "grasscolor"):
            self.grasscolor = list(self.load_image("assets/minecraft/textures/colormap/grass.png").getdata())
        return self.grasscolor

    def load_foliage_color(self):
        """Helper function to load the foliage color texture."""
        if not hasattr(self, "foliagecolor"):
            self.foliagecolor = list(self.load_image("assets/minecraft/textures/colormap/foliage.png").getdata())
        return self.foliagecolor

    #I guess "watercolor" is wrong. But I can't correct as my texture pack don't define water color.
    def load_water_color(self):
        """Helper function to load the water color texture."""
        if not hasattr(self, "watercolor"):
            self.watercolor = list(self.load_image("watercolor.png").getdata())
        return self.watercolor

    def _split_terrain(self, terrain):
        """Builds and returns a length 256 array of each 16x16 chunk
        of texture.
        """
        textures = []
        (terrain_width, terrain_height) = terrain.size
        texture_resolution = terrain_width / 16
        for y in range(16):
            for x in range(16):
                left = x*texture_resolution
                upper = y*texture_resolution
                right = left+texture_resolution
                lower = upper+texture_resolution
                region = terrain.transform(
                          (16, 16),
                          Image.EXTENT,
                          (left,upper,right,lower),
                          Image.BICUBIC)
                textures.append(region)

        return textures

    ##
    ## Image Transformation Functions
    ##

    @staticmethod
    def transform_image_top(img):
        """Takes a PIL image and rotates it left 45 degrees and shrinks the y axis
        by a factor of 2. Returns the resulting image, which will be 24x12 pixels

        """

        # Resize to 17x17, since the diagonal is approximately 24 pixels, a nice
        # even number that can be split in half twice
        img = img.resize((17, 17), Image.ANTIALIAS)

        # Build the Affine transformation matrix for this perspective
        transform = numpy.matrix(numpy.identity(3))
        # Translate up and left, since rotations are about the origin
        transform *= numpy.matrix([[1,0,8.5],[0,1,8.5],[0,0,1]])
        # Rotate 45 degrees
        ratio = math.cos(math.pi/4)
        #transform *= numpy.matrix("[0.707,-0.707,0;0.707,0.707,0;0,0,1]")
        transform *= numpy.matrix([[ratio,-ratio,0],[ratio,ratio,0],[0,0,1]])
        # Translate back down and right
        transform *= numpy.matrix([[1,0,-12],[0,1,-12],[0,0,1]])
        # scale the image down by a factor of 2
        transform *= numpy.matrix("[1,0,0;0,2,0;0,0,1]")

        transform = numpy.array(transform)[:2,:].ravel().tolist()

        newimg = img.transform((24,12), Image.AFFINE, transform)
        return newimg

    @staticmethod
    def transform_image_side(img):
        """Takes an image and shears it for the left side of the cube (reflect for
        the right side)"""

        # Size of the cube side before shear
        img = img.resize((12,12), Image.ANTIALIAS)

        # Apply shear
        transform = numpy.matrix(numpy.identity(3))
        transform *= numpy.matrix("[1,0,0;-0.5,1,0;0,0,1]")

        transform = numpy.array(transform)[:2,:].ravel().tolist()

        newimg = img.transform((12,18), Image.AFFINE, transform)
        return newimg

    @staticmethod
    def transform_image_slope(img):
        """Takes an image and shears it in the shape of a slope going up
        in the -y direction (reflect for +x direction). Used for minetracks"""

        # Take the same size as trasform_image_side
        img = img.resize((12,12), Image.ANTIALIAS)

        # Apply shear
        transform = numpy.matrix(numpy.identity(3))
        transform *= numpy.matrix("[0.75,-0.5,3;0.25,0.5,-3;0,0,1]")
        transform = numpy.array(transform)[:2,:].ravel().tolist()

        newimg = img.transform((24,24), Image.AFFINE, transform)

        return newimg

    @staticmethod
    def transform_image_angle(img, angle):
        """Takes an image an shears it in arbitrary angle with the axis of
        rotation being vertical.

        WARNING! Don't use angle = pi/2 (or multiplies), it will return
        a blank image (or maybe garbage).

        NOTE: angle is in the image not in game, so for the left side of a
        block angle = 30 degree.
        """

        # Take the same size as trasform_image_side
        img = img.resize((12,12), Image.ANTIALIAS)

        # some values
        cos_angle = math.cos(angle)
        sin_angle = math.sin(angle)

        # function_x and function_y are used to keep the result image in the 
        # same position, and constant_x and constant_y are the coordinates
        # for the center for angle = 0.
        constant_x = 6.
        constant_y = 6.
        function_x = 6.*(1-cos_angle)
        function_y = -6*sin_angle
        big_term = ( (sin_angle * (function_x + constant_x)) - cos_angle* (function_y + constant_y))/cos_angle

        # The numpy array is not really used, but is helpful to 
        # see the matrix used for the transformation.
        transform = numpy.array([[1./cos_angle, 0, -(function_x + constant_x)/cos_angle],
                                 [-sin_angle/(cos_angle), 1., big_term ],
                                 [0, 0, 1.]])

        transform = tuple(transform[0]) + tuple(transform[1])

        newimg = img.transform((24,24), Image.AFFINE, transform)

        return newimg

    def build_block(self, top, side):
        """From a top texture and a side texture, build a block image.
        top and side should be 16x16 image objects. Returns a 24x24 image

        """
        img = Image.new("RGBA", (24,24), self.bgcolor)

        original_texture = top.copy()
        top = self.transform_image_top(top)

        if not side:
            alpha_over(img, top, (0,0), top)
            return img

        side = self.transform_image_side(side)
        otherside = side.transpose(Image.FLIP_LEFT_RIGHT)

        # Darken the sides slightly. These methods also affect the alpha layer,
        # so save them first (we don't want to "darken" the alpha layer making
        # the block transparent)
        sidealpha = side.split()[3]
        side = ImageEnhance.Brightness(side).enhance(0.9)
        side.putalpha(sidealpha)
        othersidealpha = otherside.split()[3]
        otherside = ImageEnhance.Brightness(otherside).enhance(0.8)
        otherside.putalpha(othersidealpha)

        alpha_over(img, top, (0,0), top)
        alpha_over(img, side, (0,6), side)
        alpha_over(img, otherside, (12,6), otherside)

        # Manually touch up 6 pixels that leave a gap because of how the
        # shearing works out. This makes the blocks perfectly tessellate-able
        for x,y in [(13,23), (17,21), (21,19)]:
            # Copy a pixel to x,y from x-1,y
            img.putpixel((x,y), img.getpixel((x-1,y)))
        for x,y in [(3,4), (7,2), (11,0)]:
            # Copy a pixel to x,y from x+1,y
            img.putpixel((x,y), img.getpixel((x+1,y)))

        return img

    def build_slab_block(self, top, side, upper):
        """From a top texture and a side texture, build a slab block image.
        top and side should be 16x16 image objects. Returns a 24x24 image

        """
        # cut the side texture in half
        mask = side.crop((0,8,16,16))
        side = Image.new(side.mode, side.size, self.bgcolor)
        alpha_over(side, mask,(0,0,16,8), mask)

        # plain slab
        top = self.transform_image_top(top)
        side = self.transform_image_side(side)
        otherside = side.transpose(Image.FLIP_LEFT_RIGHT)

        sidealpha = side.split()[3]
        side = ImageEnhance.Brightness(side).enhance(0.9)
        side.putalpha(sidealpha)
        othersidealpha = otherside.split()[3]
        otherside = ImageEnhance.Brightness(otherside).enhance(0.8)
        otherside.putalpha(othersidealpha)

        # upside down slab
        delta = 0
        if upper:
            delta = 6

        img = Image.new("RGBA", (24,24), self.bgcolor)
        alpha_over(img, side, (0,12 - delta), side)
        alpha_over(img, otherside, (12,12 - delta), otherside)
        alpha_over(img, top, (0,6 - delta), top)

        # Manually touch up 6 pixels that leave a gap because of how the
        # shearing works out. This makes the blocks perfectly tessellate-able
        if upper:
            for x,y in [(3,4), (7,2), (11,0)]:
                # Copy a pixel to x,y from x+1,y
                img.putpixel((x,y), img.getpixel((x+1,y)))
            for x,y in [(13,17), (17,15), (21,13)]:
                # Copy a pixel to x,y from x-1,y
                img.putpixel((x,y), img.getpixel((x-1,y)))
        else:
            for x,y in [(3,10), (7,8), (11,6)]:
                # Copy a pixel to x,y from x+1,y
                img.putpixel((x,y), img.getpixel((x+1,y)))
            for x,y in [(13,23), (17,21), (21,19)]:
                # Copy a pixel to x,y from x-1,y
                img.putpixel((x,y), img.getpixel((x-1,y)))

        return img

    def build_full_block(self, top, side1, side2, side3, side4, bottom=None):
        """From a top texture, a bottom texture and 4 different side textures,
        build a full block with four differnts faces. All images should be 16x16 
        image objects. Returns a 24x24 image. Can be used to render any block.

        side1 is in the -y face of the cube     (top left, east)
        side2 is in the +x                      (top right, south)
        side3 is in the -x                      (bottom left, north)
        side4 is in the +y                      (bottom right, west)

        A non transparent block uses top, side 3 and side 4.

        If top is a tuple then first item is the top image and the second
        item is an increment (integer) from 0 to 16 (pixels in the
        original minecraft texture). This increment will be used to crop the
        side images and to paste the top image increment pixels lower, so if
        you use an increment of 8, it will draw a half-block.

        NOTE: this method uses the bottom of the texture image (as done in 
        minecraft with beds and cakes)

        """

        increment = 0
        if isinstance(top, tuple):
            increment = int(round((top[1] / 16.)*12.)) # range increment in the block height in pixels (half texture size)
            crop_height = increment
            top = top[0]
            if side1 is not None:
                side1 = side1.copy()
                ImageDraw.Draw(side1).rectangle((0, 0,16,crop_height),outline=(0,0,0,0),fill=(0,0,0,0))
            if side2 is not None:
                side2 = side2.copy()
                ImageDraw.Draw(side2).rectangle((0, 0,16,crop_height),outline=(0,0,0,0),fill=(0,0,0,0))
            if side3 is not None:
                side3 = side3.copy()
                ImageDraw.Draw(side3).rectangle((0, 0,16,crop_height),outline=(0,0,0,0),fill=(0,0,0,0))
            if side4 is not None:
                side4 = side4.copy()
                ImageDraw.Draw(side4).rectangle((0, 0,16,crop_height),outline=(0,0,0,0),fill=(0,0,0,0))

        img = Image.new("RGBA", (24,24), self.bgcolor)

        # first back sides
        if side1 is not None :
            side1 = self.transform_image_side(side1)
            side1 = side1.transpose(Image.FLIP_LEFT_RIGHT)

            # Darken this side.
            sidealpha = side1.split()[3]
            side1 = ImageEnhance.Brightness(side1).enhance(0.9)
            side1.putalpha(sidealpha)        

            alpha_over(img, side1, (0,0), side1)


        if side2 is not None :
            side2 = self.transform_image_side(side2)

            # Darken this side.
            sidealpha2 = side2.split()[3]
            side2 = ImageEnhance.Brightness(side2).enhance(0.8)
            side2.putalpha(sidealpha2)

            alpha_over(img, side2, (12,0), side2)

        if bottom is not None :
            bottom = self.transform_image_top(bottom)
            alpha_over(img, bottom, (0,12), bottom)

        # front sides
        if side3 is not None :
            side3 = self.transform_image_side(side3)

            # Darken this side
            sidealpha = side3.split()[3]
            side3 = ImageEnhance.Brightness(side3).enhance(0.9)
            side3.putalpha(sidealpha)

            alpha_over(img, side3, (0,6), side3)

        if side4 is not None :
            side4 = self.transform_image_side(side4)
            side4 = side4.transpose(Image.FLIP_LEFT_RIGHT)

            # Darken this side
            sidealpha = side4.split()[3]
            side4 = ImageEnhance.Brightness(side4).enhance(0.8)
            side4.putalpha(sidealpha)

            alpha_over(img, side4, (12,6), side4)

        if top is not None :
            top = self.transform_image_top(top)
            alpha_over(img, top, (0, increment), top)

        # Manually touch up 6 pixels that leave a gap because of how the
        # shearing works out. This makes the blocks perfectly tessellate-able
        for x,y in [(13,23), (17,21), (21,19)]:
            # Copy a pixel to x,y from x-1,y
            img.putpixel((x,y), img.getpixel((x-1,y)))
        for x,y in [(3,4), (7,2), (11,0)]:
            # Copy a pixel to x,y from x+1,y
            img.putpixel((x,y), img.getpixel((x+1,y)))

        return img

    def build_sprite(self, side):
        """From a side texture, create a sprite-like texture such as those used
        for spiderwebs or flowers."""
        img = Image.new("RGBA", (24,24), self.bgcolor)

        side = self.transform_image_side(side)
        otherside = side.transpose(Image.FLIP_LEFT_RIGHT)

        alpha_over(img, side, (6,3), side)
        alpha_over(img, otherside, (6,3), otherside)
        return img

    def build_billboard(self, tex):
        """From a texture, create a billboard-like texture such as
        those used for tall grass or melon stems.
        """
        img = Image.new("RGBA", (24,24), self.bgcolor)

        front = tex.resize((14, 12), Image.ANTIALIAS)
        alpha_over(img, front, (5,9))
        return img

    def generate_opaque_mask(self, img):
        """ Takes the alpha channel of the image and generates a mask
        (used for lighting the block) that deprecates values of alpha
        smallers than 50, and sets every other value to 255. """

        alpha = img.split()[3]
        return alpha.point(lambda a: int(min(a, 25.5) * 10))

    def tint_texture(self, im, c):
        # apparently converting to grayscale drops the alpha channel?
        i = ImageOps.colorize(ImageOps.grayscale(im), (0,0,0), c)
        i.putalpha(im.split()[3]); # copy the alpha band back in. assuming RGBA
        return i

    def generate_texture_tuple(self, img):
        """ This takes an image and returns the needed tuple for the
        blockmap array."""
        if img is None:
            return None
        return (img, self.generate_opaque_mask(img))

    #
    # This part if for reading the models/*.json assets. 
    #

    models = {}

    def load_model(self, modelname):
        if modelname in self.models:
            return self.models[modelname]

        fileobj = self.find_file('assets/minecraft/models/' + modelname +
                                 '.json', verbose=logging.getLogger().isEnabledFor(logging.DEBUG))
        self.models[modelname] = json.load(fileobj)
        fileobj.close()

        if 'parent' in self.models[modelname]:
            parent = self.load_model(re.sub('.*:', '', self.models[modelname]['parent']))
            if 'textures' in parent:
                self.models[modelname]['textures'].update(parent['textures'])
            if 'elements' in parent:
                if 'elements' in self.models[modelname]:
                    self.models[modelname]['elements'] += parent['elements']
                else:
                    self.models[modelname]['elements'] = parent['elements']
            del self.models[modelname]['parent']

        self.models[modelname] = self.normalize_model(modelname)
        return self.models[modelname]

    # fix known inconsistencies in model info
    def normalize_model(self, modelname):
        match modelname:
            case 'block/observer':
                self.models[modelname] = deepcopy(self.models[modelname])
                self.models[modelname]['elements'][0]['faces']['up']['uv'] = [0, 0, 16, 16]
            # remap textures of blocks with the rotation property to match the mapping of the observer textures
            case 'block/loom':
                self.models[modelname] = deepcopy(self.models[modelname])
                self.models[modelname]['elements'][0]['faces']['up']['texturerotation'] = 180
                self.models[modelname]['elements'][0]['faces']['down']['texturerotation'] = 180
            case 'block/barrel' | 'block/barrel_open':
                self.models[modelname] = deepcopy(self.models[modelname])
                self.models[modelname]['elements'][0]['faces']['north']['texture'] = '#up'
                self.models[modelname]['elements'][0]['faces']['south']['texture'] = '#down'
                self.models[modelname]['elements'][0]['faces']['down']['texture'] = '#north'
                self.models[modelname]['elements'][0]['faces']['up']['texture'] = '#south'
                self.models[modelname]['elements'][0]['faces']['east']['texturerotation'] = 90
                self.models[modelname]['elements'][0]['faces']['west']['texturerotation'] = 90
            case 'block/dropper_vertical' | 'block/dispenser_vertical':
                self.models[modelname] = deepcopy(self.models[modelname])
                self.models[modelname]['elements'][0]['faces']['north']['texture'] = '#up'
                self.models[modelname]['elements'][0]['faces']['up']['texture'] = '#north'

        return self.models[modelname]

    def build_block_from_model(self, modelname, blockstate={}):
        modelname = 'block/' + modelname

        colmodel = self.load_model(modelname)

        if 'elements' not in colmodel:
            return None

        elements = colmodel['elements']
        elements.sort(key=lambda x: (x['to'][1], 16 - (x['from'][0]+x['to'][0]),16 - (x['from'][2]+x['to'][2])))

        img = Image.new("RGBA", (24, 24), self.bgcolor)

        # for each elements
        for elem in elements:
            try:
                if 'west' in elem['faces']:
                    self.draw_blockface(img, elem, colmodel, blockstate, modelname, 'west')
                elif 'east' in elem['faces']:
                    self.draw_blockface(img, elem, colmodel, blockstate, modelname, 'east')

                if 'north' in elem['faces']:
                    self.draw_blockface(img, elem, colmodel, blockstate, modelname, 'north')
                elif 'south' in elem['faces']:
                    self.draw_blockface(img, elem, colmodel, blockstate, modelname, 'south')

                if 'up' in elem['faces']:
                    self.draw_blockface(img, elem, colmodel, blockstate, modelname, 'up')
                elif 'down' in elem['faces']:
                    self.draw_blockface(img, elem, colmodel, blockstate, modelname, 'down')
            except KeyError as e:
                continue

        # Manually touch up 6 pixels that leave a gap because of how the
        # shearing works out. This makes the blocks perfectly tessellate-able
        for x, y in [(13, 23), (17, 21), (21, 19)]:
            # Copy a pixel to x,y from x-1,y
            img.putpixel((x, y), img.getpixel((x - 1, y)))
        for x, y in [(3, 4), (7, 2), (11, 0)]:
            # Copy a pixel to x,y from x+1,y
            img.putpixel((x, y), img.getpixel((x + 1, y)))

        return img

    def draw_blockface(self, img, elem, colmodel, blockstate, modelname, direction):
        if 'facing' in blockstate:
            facing = self.map_facing_to_real(blockstate['facing'], direction)
        elif 'axis' in blockstate:
            facing = self.map_axis_to_real(blockstate['axis'], direction)
        else:
            facing = direction
        texture = self.build_texture(direction, elem, colmodel, blockstate, modelname, facing)
        alpha_over(img, texture, self.image_pos(direction, elem, facing, modelname), texture)

    def image_pos(self, elementdirection, element, facing, modelname):

        match elementdirection:

            # case 'west': return (12, 6)
            case 'east': return (0, 0)
            # case 'north': return(0, 6)
            case 'south': return (12, 0)
            # case 'up': return (0, 0)
            # case 'down': return (0, 12)

            # # 24x24 image
            ## WIP
            # Down and to the right
            # 16 => 12,0
            # 0  => 6,6
            # case 'south':
            #     toxa = 12
            #     tox = 0
            # #     if 'to' in element:
            # #         toxa = int(math.ceil(6 + (6/16 * element[face[0]][face[1]])))
            # #         tox = int(math.ceil(6 - (6/16 * element[face[0]][face[1]])))
            #     return (toxa, tox) # 12,0

            # # # # Up and to the right
            # # # # 0  => 0,6
            # # # # 16 => 12,0
            case 'west':
                setback = 16 - self.setback(element, facing)
                toya = int(math.ceil(12/16 * (setback)))
                toy = int(math.ceil(6/16 * (setback)))
                return (toya, toy) # 12, 6

            # Up and to the left
            # 16  => 12,0
            # 0 => 0,6
            case 'north':
                setback = self.setback(element, facing)
                toya = int(math.ceil((12/16 * (setback))))
                toy = int(math.ceil(6 - (6/16 * (setback))))
                return (toya, toy) # 0, 6

            # # # # Down and to the right
            # # # # 0  => 0,0
            # # # # 16 => 6,6
            # case 'east':
            #     setback = self.setback(element, facing)
            #     toya = int(math.ceil(6/16 * (setback)))
            #     toy = int(math.ceil(6/16 * (setback)))
            #     return (toya, toy) # 0, 0

            # # move up
            case 'down':
                fromy = 12
                if 'from' in element:
                    fromy = int(math.ceil(((16 - element['from'][1])/16*12.)))
                return (0, fromy) # 0,0
            
            # # move down
            case 'up' | _: 
                toy = 0
                if 'to' in element:
                    toy = int(math.ceil(((16 - element['to'][1])/16*12.)))
                return (0, toy) # 0,6

    def setback(self, element, facing):
        return {'up': 16, 'down': 0,
                    'north': element['from'][2], 'south': 16 - element['to'][2],
                    'east': 16 - element['to'][0], 'west': element['from'][0]}[facing]

    def numvalue_orientation(self, orientation):
        return {'south': 0, 'west': 1, 'north': 2, 'east': 3, 'up': 4, 'down': 6}[orientation]
        
    def orientation_from_numvalue(self, orientation):
        return {0: 'south', 1: 'west', 2: 'north', 3: 'east', 4: 'up', 6: 'down'}[orientation]
        
    # translates rotation to real face value
    # facing is the blockproperty
    # targetfacing is the direction in witch the north is rotated
    def map_facing_to_real(self, blockfacing, targetblockface):
        resultface = blockfacing
        if blockfacing == 'up':
            if targetblockface == 'up':
                resultface = 'north'
            elif targetblockface == 'north':
                resultface = 'west'
            elif targetblockface == 'west':
                resultface = 'down'
        elif blockfacing == 'down':
            if targetblockface == 'west':
                resultface = 'down'
            elif targetblockface == 'north':
                resultface = 'west'
            elif targetblockface == 'up':
                resultface = 'south'
        elif targetblockface == 'up':
            resultface = 'up'
        elif targetblockface == 'down':
            resultface = 'down'
        else:
            resultface = self.orientation_from_numvalue((self.numvalue_orientation(
                targetblockface) + [0, 3, 2, 1][self.numvalue_orientation(blockfacing)] + 1 + self.rotation + (self.rotation % 2) * 2) % 4)
        return resultface

    def map_axis_to_real(self, axis, textureface):
        match axis:
            case 'x':
                return {'up': 'north', 'north': 'down', 'down': 'south', 'south': 'up', 'east': 'east', 'west': 'west'}[textureface]
            case 'z':
                return {'up': 'west', 'west': 'down', 'down': 'east', 'east': 'up', 'north': 'north', 'south': 'south'}[textureface]
            case 'y' | _:
                return textureface

    def axis_rotation(self, axis, face, texture):
        match axis:
            case 'x':
                rotation = {'up': 270, 'north': 0, 'down': 0,
                            'south': 0, 'east': 270, 'west': 270}[face]
                return texture.rotate(rotation)
            case 'z':
                rotation = {'up': 0, 'west': 0, 'down': 0,
                            'east': 0, 'north': 90, 'south': 90}[face]
                return texture.rotate(rotation)
            case 'y' | _:
                return texture
        
    def build_texture(self, direction, elem, data, blockstate, modelname, textureface):   

        texture = self.find_texture_from_model(
            elem['faces'][textureface]['texture'], data['textures']).copy()

        if 'axis' in blockstate:
            texture = self.axis_rotation(blockstate['axis'], direction, texture)
        texture = self.texture_rotation(direction, texture, blockstate, elem['faces'][textureface], modelname)
            
        if 'from' in elem and 'to' in elem and (elem['from'] != [0,0,0] or elem['to'] != [16,16,16]):
            area = [0,0,16,16]
            
            match textureface:

                case 'west': 
                    area = [elem['from'][2],elem['from'][1],elem['to'][2],elem['to'][1]]
                    texture = self.crop_to_transparancy(texture, area)
                case 'east':
                    area = [16-elem['from'][2],elem['from'][1],16-elem['to'][2],elem['to'][1]]
                    texture = self.crop_to_transparancy(texture, area)
                case 'north':
                    area = [16-elem['from'][0],elem['from'][1],16-elem['to'][0],elem['to'][1]]
                    texture = self.crop_to_transparancy(texture, area)
                case 'south':
                    area = [elem['from'][0],elem['from'][1],elem['to'][0],elem['to'][1]]
                    texture = self.crop_to_transparancy(texture, area)
                case 'up'|'down'|_:
                    if 'facing' in blockstate and blockstate['facing'] in {'north'}:
                        area = [elem['from'][0],16-elem['from'][2],elem['to'][0],16-elem['to'][2]]
                    elif 'facing' in blockstate and blockstate['facing'] in {'south'}:
                        area = [16-elem['from'][0],elem['from'][2],16-elem['to'][0],elem['to'][2]]
                    elif 'facing' in blockstate and blockstate['facing'] in {'west'}:
                        area = [elem['from'][2],elem['from'][0],elem['to'][2],elem['to'][0]]
                    elif 'facing' in blockstate and blockstate['facing'] in {'east'}:
                        area = [16-elem['from'][2],16-elem['from'][0],16-elem['to'][2],16-elem['to'][0]]
                    else:
                        area = [elem['from'][2],elem['from'][0],elem['to'][2],elem['to'][0]]
                    texture = self.crop_to_transparancy(texture, area)

        # TODO: deal with rotation

        texture = self.transform_texture(direction, texture, blockstate, elem['faces'][textureface])
        texture = self.adjust_lighting(direction, texture)

        return texture

    def crop_to_transparancy(self, img, area):
        # PIL image coordinates do not match MC texture coordinates
        # PIL starts in lower left
        # MC starts in upper left
        # r, b, l, t

        if area[0] > area[2]:
            area = [area[2], area[1], area[0], area[3]]
        if area[1] > area[3]:
            area = [area[0], area[3], area[2], area[1]]
        if area == [ 0, 0, 16, 16 ]:
            return img

        # cut from top
        if area[3] != 16:
            ImageDraw.Draw(img).rectangle((0, 0, 16, 16 - area[3]-1), outline=(0, 0, 0, 0), fill=(0, 0, 0, 0))

        # cut from bottom
        if area[1] != 0:
            ImageDraw.Draw(img).rectangle((0, 16 - (area[1]-2), 16, 16), outline=(0, 0, 0, 0), fill=(0, 0, 0, 0))
        
        # cut from right
        if area[2] != 16:
            ImageDraw.Draw(img).rectangle((area[2]-1, 0, 16, 16), outline=(0, 0, 0, 0), fill=(0, 0, 0, 0))
        
        # cut from left
        if area[0] != 0:
            ImageDraw.Draw(img).rectangle((0, 0, area[0]-2, 16), outline=(0, 0, 0, 0), fill=(0, 0, 0, 0))
        
        return img

    def adjust_lighting(self, direction, texture):
        match direction:
            case 'south' | 'west':
                sidealpha = texture.split()[3]
                texture = ImageEnhance.Brightness(texture).enhance(0.8)
                texture.putalpha(sidealpha)
                return texture
            case 'north' | 'east':
                sidealpha = texture.split()[3]
                texture = ImageEnhance.Brightness(texture).enhance(0.9)
                texture.putalpha(sidealpha)
                return texture
            case 'down' | 'up' | _:
                return texture

    def texture_rotation(self, direction, texture, blockstate, faceinfo, modelname):
        rotation = 0
        if 'texturerotation' in faceinfo:
            rotation += faceinfo['texturerotation']
        match direction:
            case 'down' | 'up':
                if 'facing' in blockstate:
                    if self.numvalue_orientation(blockstate['facing']) < 4:
                        rotation += [0, 270, 180,
                                     90][(self.numvalue_orientation(blockstate['facing']) + self.rotation) % 4]
                    else:
                        rotation += [180, 0][({'up': 0, 'down': 1}[blockstate['facing']])]
            case 'north' | 'south':
                if 'rotation' in faceinfo:
                    rotation = {0: 180, 90: 90, 180: 0, 270: 270}[faceinfo['rotation']]
                if 'facing' in blockstate and blockstate['facing'] in {'up', 'down'}:
                    rotation += [90, 90][({'up': 0, 'down': 1}[blockstate['facing']])]
            case 'west' | 'east':
                if 'rotation' in faceinfo:
                    rotation = {0: 180, 90: 270, 180: 0, 270: 90}[faceinfo['rotation']]
                if 'facing' in blockstate and blockstate['facing'] in {'up', 'down'}:
                    rotation += [180, 0][({'up': 0, 'down': 1}[blockstate['facing']])]
        
        return texture.rotate(rotation % 360)

    def transform_texture(self, direction, texture, blockstate, faceinfo):
        match direction:
            case 'down' | 'up':
                return self.transform_image_top(texture)
            case 'north' | 'south':
                texture = self.transform_image_side(texture)
            case 'west' | 'east':
                texture = texture.transpose(Image.FLIP_LEFT_RIGHT)
                texture = self.transform_image_side(texture)
                texture = texture.transpose(Image.FLIP_LEFT_RIGHT)
        
        return texture

    def find_texture_from_model(self, face, textureset):
        if face.startswith('#'):
            return self.find_texture_from_model(textureset[face[1:]], textureset)
        else:
            return self.load_image_texture("assets/minecraft/textures/" + re.sub('.*:', '', face) + '.png')
    
##
## The other big one: @material and associated framework
##


# the material registration decorator
def material(blockid=[], data=[0], **kwargs):
    # mapping from property name to the set to store them in
    properties = {"transparent": transparent_blocks, "solid": solid_blocks,
                  "fluid": fluid_blocks, "nospawn": nospawn_blocks}
    
    # make sure blockid and data are iterable
    try:
        iter(blockid)
    except Exception:
        blockid = [blockid,]
    try:
        iter(data)
    except Exception:
        data = [data,]
        
    def inner_material(func):
        global blockmap_generators
        global max_data, max_blockid

        # create a wrapper function with a known signature
        @functools.wraps(func)
        def func_wrapper(texobj, blockid, data):
            return func(texobj, blockid, data)
        
        used_datas.update(data)
        if max(data) >= max_data:
            max_data = max(data) + 1
        
        for block in blockid:
            # set the property sets appropriately
            known_blocks.update([block])
            if block >= max_blockid:
                max_blockid = block + 1
            for prop in properties:
                try:
                    if block in kwargs.get(prop, []):
                        properties[prop].update([block])
                except TypeError:
                    if kwargs.get(prop, False):
                        properties[prop].update([block])

            # populate blockmap_generators with our function
            for d in data:
                blockmap_generators[(block, d)] = func_wrapper
        
        return func_wrapper
    return inner_material


def solidmodelblock(blockid=[], name=None, **kwargs):
    new_kwargs = {'solid': True}
    new_kwargs.update(kwargs)
    return modelblock(blockid=blockid, name=name, **new_kwargs)


def modelblock(blockid=[], name=None, **kwargs):    
    if name is None:
        raise ValueError("block name was not provided")
    
    @material(blockid=blockid, **kwargs)
    def inner_block(self, unused_id, unused_data):
        return self.build_block_from_model(name)
    return inner_block

# shortcut function for sprite blocks, defaults to transparent
def sprite(blockid=[], imagename=None, **kwargs):
    new_kwargs = {'transparent' : True}
    new_kwargs.update(kwargs)
    
    if imagename is None:
        raise ValueError("imagename was not provided")
    
    @material(blockid=blockid, **new_kwargs)
    def inner_sprite(self, unused_id, unused_data):
        return self.build_sprite(self.load_image_texture(imagename))
    return inner_sprite

# shortcut function for billboard blocks, defaults to transparent
def billboard(blockid=[], imagename=None, **kwargs):
    new_kwargs = {'transparent' : True}
    new_kwargs.update(kwargs)
    
    if imagename is None:
        raise ValueError("imagename was not provided")
    
    @material(blockid=blockid, **new_kwargs)
    def inner_billboard(self, unused_id, unused_data):
        return self.build_billboard(self.load_image_texture(imagename))
    return inner_billboard


def unbound_models():
    global max_blockid, block_models, next_unclaimed_id
    tex = Textures()

    models = tex.find_models(tex)
    for model in models:
        # determine transparency
        
        colmodel = tex.load_model('block/' + model)
        if 'elements' not in colmodel:
            continue
        transp = False

        # for each elements
        for elem in colmodel['elements']:
            if 'from' in elem and 'to' in elem and (elem['from'] != [0,0,0] or elem['to'] != [16,16,16]):
                transp = True
                break

        # find next unclaimed id to keep id values as low as possible
        while (next_unclaimed_id, 0) in blockmap_generators:
            next_unclaimed_id = next_unclaimed_id + 1
            # 1792 to 2047 is a reserved range for wall blocks
            if next_unclaimed_id < 2048 and next_unclaimed_id > 1791:
                next_unclaimed_id = 2048
        id = next_unclaimed_id
        modelblock(blockid=id, name=model, transparent=transp, solid=True)
        block_models['minecraft:' + model] = (id, 0)

##
## the texture definitions for all blocks that have special rendering conditions
##


@material(blockid=2, data=list(range(11)) + [0x10, ], solid=True)
def grass(self, blockid, data):
    if data & 0x10:
        return self.build_block_from_model('grass_block_snow')
    else:
        side_img = self.load_image_texture("assets/minecraft/textures/block/grass_block_side.png")
        img = self.build_block(self.load_image_texture(
            "assets/minecraft/textures/block/grass_block_top.png"), side_img)
        alpha_over(img, self.biome_grass_texture, (0, 0), self.biome_grass_texture)
        return img


@material(blockid=6, data=list(range(16)), transparent=True)
def saplings(self, blockid, data):
    # usual saplings
    tex = self.load_image_texture("assets/minecraft/textures/block/oak_sapling.png")
    
    if data & 0x3 == 1: # spruce sapling
        tex = self.load_image_texture("assets/minecraft/textures/block/spruce_sapling.png")
    elif data & 0x3 == 2: # birch sapling
        tex = self.load_image_texture("assets/minecraft/textures/block/birch_sapling.png")
    elif data & 0x3 == 3: # jungle sapling
        tex = self.load_image_texture("assets/minecraft/textures/block/jungle_sapling.png")
    elif data & 0x3 == 4: # acacia sapling
        tex = self.load_image_texture("assets/minecraft/textures/block/acacia_sapling.png")
    elif data & 0x3 == 5: # dark oak/roofed oak/big oak sapling
        tex = self.load_image_texture("assets/minecraft/textures/block/dark_oak_sapling.png")
    return self.build_sprite(tex)


sprite(blockid=11385, imagename="assets/minecraft/textures/block/oak_sapling.png")
sprite(blockid=11386, imagename="assets/minecraft/textures/block/spruce_sapling.png")
sprite(blockid=11387, imagename="assets/minecraft/textures/block/birch_sapling.png")
sprite(blockid=11388, imagename="assets/minecraft/textures/block/jungle_sapling.png")
sprite(blockid=11389, imagename="assets/minecraft/textures/block/acacia_sapling.png")
sprite(blockid=11390, imagename="assets/minecraft/textures/block/dark_oak_sapling.png")

sprite(blockid=11413, imagename="assets/minecraft/textures/block/bamboo_stage0.png")

# bedrock
solidmodelblock(blockid=7, name='bedrock')

# water, glass, and ice (no inner surfaces)
# uses pseudo-ancildata found in iterate.c
@material(blockid=[8, 9, 20, 79, 95], data=list(range(512)), fluid=(8, 9), transparent=True, nospawn=True, solid=(79, 20, 95))
def no_inner_surfaces(self, blockid, data):
    if blockid == 8 or blockid == 9:
        texture = self.load_water()
    elif blockid == 20:
        texture = self.load_image_texture("assets/minecraft/textures/block/glass.png")
    elif blockid == 95:
        texture = self.load_image_texture("assets/minecraft/textures/block/%s_stained_glass.png" % color_map[data & 0x0f])
    else:
        texture = self.load_image_texture("assets/minecraft/textures/block/ice.png")

    # now that we've used the lower 4 bits to get color, shift down to get the 5 bits that encode face hiding
    if not (blockid == 8 or blockid == 9): # water doesn't have a shifted pseudodata
        data = data >> 4

    if (data & 0b10000) == 16:
        top = texture
    else:
        top = None
            
    if (data & 0b0010) == 2:
        side3 = texture    # bottom left    
    else:
        side3 = None
    
    if (data & 0b0100) == 4:
        side4 = texture    # bottom right
    else:
        side4 = None
    
    # if nothing shown do not draw at all
    if top is None and side3 is None and side4 is None:
        return None
    
    img = self.build_full_block(top,None,None,side3,side4)
    return img

@material(blockid=[10, 11], data=list(range(16)), fluid=True, transparent=False, nospawn=True)
def lava(self, blockid, data):
    lavatex = self.load_lava()
    return self.build_block(lavatex, lavatex)


# Mineral overlay blocks
# gold ore
solidmodelblock(blockid=14, name='gold_ore')
# iron ore
solidmodelblock(blockid=15, name='iron_ore')
# coal ore
solidmodelblock(blockid=16, name='coal_ore')


@material(blockid=[17, 162, 11306, 11307, 11308, 11309, 11310, 11311, 1008, 1009, 1126],
          data=list(range(12)), solid=True)
def wood(self, blockid, data):
    type = data & 3
    
    blockstate = {}
    blockstate['axis'] = {0: 'y', 4: 'x', 8: 'z'}[data & 12]

    if blockid == 17:
        if type == 0:
            return self.build_block_from_model('oak_log', blockstate)
        if type == 1:
            return self.build_block_from_model('spruce_log', blockstate)
        if type == 2:
            return self.build_block_from_model('birch_log', blockstate)
        if type == 3:
            return self.build_block_from_model('jungle_log', blockstate)

    if blockid == 162:
        if type == 0:
            return self.build_block_from_model('acacia_log', blockstate)
        if type == 1:
            return self.build_block_from_model('dark_oak_log', blockstate)

    if blockid == 11306:
        if type == 0:
            return self.build_block_from_model('stripped_oak_log', blockstate)
        if type == 1:
            return self.build_block_from_model('stripped_spruce_log', blockstate)
        if type == 2:
            return self.build_block_from_model('stripped_birch_log', blockstate)
        if type == 3:
            return self.build_block_from_model('stripped_jungle_log', blockstate)

    if blockid == 11307:
        if type == 0:
            return self.build_block_from_model('stripped_acacia_log', blockstate)
        if type == 1:
            return self.build_block_from_model('stripped_dark_oak_log', blockstate)

    if blockid == 11308:
        if type == 0:
            return self.build_block_from_model('oak_wood', blockstate)
        if type == 1:
            return self.build_block_from_model('spruce_wood', blockstate)
        if type == 2:
            return self.build_block_from_model('birch_wood', blockstate)
        if type == 3:
            return self.build_block_from_model('jungle_wood', blockstate)

    if blockid == 11309:
        if type == 0:
            return self.build_block_from_model('acacia_wood', blockstate)
        if type == 1:
            return self.build_block_from_model('dark_oak_wood', blockstate)

    if blockid == 11310:
        if type == 0:
            return self.build_block_from_model('stripped_oak_wood', blockstate)
        if type == 1:
            return self.build_block_from_model('stripped_spruce_wood', blockstate)
        if type == 2:
            return self.build_block_from_model('stripped_birch_wood', blockstate)
        if type == 3:
            return self.build_block_from_model('stripped_jungle_wood', blockstate)
            
    if blockid == 11311:
        if type == 0:
            return self.build_block_from_model('stripped_acacia_wood', blockstate)
        if type == 1:
            return self.build_block_from_model('stripped_dark_oak_wood', blockstate)

    if blockid == 1008:
        if type == 0:
            return self.build_block_from_model('warped_stem', blockstate)
        if type == 1:
            return self.build_block_from_model('stripped_warped_stem', blockstate)
        if type == 2:
            return self.build_block_from_model('crimson_stem', blockstate)
        if type == 3:
            return self.build_block_from_model('stripped_crimson_stem', blockstate)

    if blockid == 1009:
        if type == 0:
            return self.build_block_from_model('warped_hyphae', blockstate)
        if type == 1:
            return self.build_block_from_model('stripped_warped_hyphae', blockstate)
        if type == 2:
            return self.build_block_from_model('crimson_hyphae', blockstate)
        if type == 3:
            return self.build_block_from_model('stripped_crimson_hyphae', blockstate)

    if blockid == 1126:
        if type == 0:
            return self.build_block_from_model('mangrove_log', blockstate)
        if type == 1:
            return self.build_block_from_model('stripped_mangrove_log', blockstate)
    
    return self.build_block_from_model('oak_log', blockstate)


@material(blockid=[18, 161], data=list(range(16)), transparent=True, solid=True)
def leaves(self, blockid, data):
    # mask out the bits 4 and 8
    # they are used for player placed and check-for-decay blocks
    data = data & 0x7
    if (blockid, data) == (18, 0):
        return self.build_block_from_model("oak_leaves")
    elif (blockid, data) == (18, 1):
        return self.build_block_from_model("spruce_leaves")
    elif (blockid, data) == (18, 2):
        return self.build_block_from_model("birch_leaves")
    elif (blockid, data) == (18, 3):
        return self.build_block_from_model("jungle_leaves")
    elif (blockid, data) == (161, 4):
        return self.build_block_from_model("acacia_leaves")
    elif (blockid, data) == (161, 5): 
        return self.build_block_from_model("dark_oak_leaves")
    elif (blockid, data) == (18, 6):
        return self.build_block_from_model("flowering_azalea_leaves")
    elif (blockid, data) == (18, 7):
        return self.build_block_from_model("azalea_leaves")
    else:
        return self.build_block_from_model("mangrove_leaves")
    

@material(blockid=19, data=list(range(2)), solid=True)
def sponge(self, blockid, data):
    if data == 0:
        return self.build_block_from_model('sponge')
    if data == 1:
        return self.build_block_from_model('wet_sponge')


# mineral overlay
# lapis lazuli ore
solidmodelblock(blockid=21, name="lapis_ore")
# lapis lazuli block
solidmodelblock(blockid=22, name="lapis_block")

@material(blockid=[577], data=list(range(8)), solid=True, transparent=True)
def modern_stairs(self, blockid, data):
    facing = {3: 'north', 0: 'east', 2: 'south', 1: 'west' }[data%4]
    return self.build_block_from_model('mangrove_stairs', {'facing': facing})

@material(blockid=[23, 158], data=list(range(6)), solid=True)
def dropper(self, blockid, data):
    facing = {0: 'down', 1: 'up', 2: 'north', 3: 'south', 4: 'west', 5: 'east'}[data]

    if blockid == 158:
        if data in {0, 1}:
            return self.build_block_from_model('dropper_vertical', {'facing': facing})
        return self.build_block_from_model('dropper', {'facing': facing})
    if blockid == 23:
        if data in {0, 1}:
            return self.build_block_from_model('dispenser_vertical', {'facing': facing})
        return self.build_block_from_model('dispenser', {'facing': facing})

# furnace, blast furnace, and smoker
@material(blockid=[61, 11362, 11364], data=list(range(14)), solid=True)
def furnaces(self, blockid, data):
    lit = data & 0b1000 == 8
    oriention = data & 0b111

    facing = {0: '', 1: '', 2: 'north', 3: 'south', 4: 'west', 5: 'east', 6: '', 7: ''}[oriention]
    if blockid == 61:
        if lit:
            return self.build_block_from_model('furnace_on', {'facing': facing})
        return self.build_block_from_model('furnace', {'facing': facing})
    if blockid == 11362:
        if lit:
            return self.build_block_from_model('blast_furnace_on', {'facing': facing})
        return self.build_block_from_model('blast_furnace', {'facing': facing})
    if blockid == 11364:
        if lit:
            return self.build_block_from_model('smoker_on', {'facing': facing})
        return self.build_block_from_model('smoker', {'facing': facing})

# Bed
@material(blockid=26, data=list(range(256)), transparent=True, nospawn=True)
def bed(self, blockid, data):
    # Bits 1-2   Rotation
    # Bit 3      Occupancy, no impact on appearance
    # Bit 4      Foot/Head of bed (0 = foot, 1 = head)
    # Bits 5-8   Color

    # first get rotation done
    # Masked to not clobber block head/foot & color info
    data = data & 0b11111100 | ((self.rotation + (data & 0b11)) % 4)

    bed_texture = self.load_image("assets/minecraft/textures/entity/bed/%s.png" % color_map[data >> 4])
    increment = 8
    left_face = None
    right_face = None
    top_face = None
    if data & 0x8 == 0x8:  # head of the bed
        top = bed_texture.copy().crop((6, 6, 22, 22))

        # Composing the side
        side = Image.new("RGBA", (16, 16), self.bgcolor)
        side_part1 = bed_texture.copy().crop((0, 6, 6, 22)).rotate(90, expand=True)
        # foot of the bed
        side_part2 = bed_texture.copy().crop((53, 3, 56, 6))
        side_part2_f = side_part2.transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(side, side_part1, (0, 7), side_part1)
        alpha_over(side, side_part2, (0, 13), side_part2)

        end = Image.new("RGBA", (16, 16), self.bgcolor)
        end_part = bed_texture.copy().crop((6, 0, 22, 6)).rotate(180)
        alpha_over(end, end_part, (0, 7), end_part)
        alpha_over(end, side_part2, (0, 13), side_part2)
        alpha_over(end, side_part2_f, (13, 13), side_part2_f)
        if data & 0x03 == 0x00:    # South
            top_face = top.rotate(180)
            left_face = side.transpose(Image.FLIP_LEFT_RIGHT)
            right_face = end
        elif data & 0x03 == 0x01:  # West
            top_face = top.rotate(90)
            left_face = end
            right_face = side.transpose(Image.FLIP_LEFT_RIGHT)
        elif data & 0x03 == 0x02:  # North
            top_face = top
            left_face = side
        elif data & 0x03 == 0x03:  # East
            top_face = top.rotate(270)
            right_face = side

    else:  # foot of the bed
        top = bed_texture.copy().crop((6, 28, 22, 44))
        side = Image.new("RGBA", (16, 16), self.bgcolor)
        side_part1 = bed_texture.copy().crop((0, 28, 6, 44)).rotate(90, expand=True)
        side_part2 = bed_texture.copy().crop((53, 3, 56, 6))
        side_part2_f = side_part2.transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(side, side_part1, (0, 7), side_part1)
        alpha_over(side, side_part2, (13, 13), side_part2)

        end = Image.new("RGBA", (16, 16), self.bgcolor)
        end_part = bed_texture.copy().crop((22, 22, 38, 28)).rotate(180)
        alpha_over(end, end_part, (0, 7), end_part)
        alpha_over(end, side_part2, (0, 13), side_part2)
        alpha_over(end, side_part2_f, (13, 13), side_part2_f)
        if data & 0x03 == 0x00:    # South
            top_face = top.rotate(180)
            left_face = side.transpose(Image.FLIP_LEFT_RIGHT)
        elif data & 0x03 == 0x01:  # West
            top_face = top.rotate(90)
            right_face = side.transpose(Image.FLIP_LEFT_RIGHT)
        elif data & 0x03 == 0x02:  # North
            top_face = top
            left_face = side
            right_face = end
        elif data & 0x03 == 0x03:  # East
            top_face = top.rotate(270)
            left_face = end
            right_face = side

    top_face = (top_face, increment)
    return self.build_full_block(top_face, None, None, left_face, right_face)

# powered, detector, activator and normal rails
@material(blockid=[27, 28, 66, 157], data=list(range(14)), transparent=True)
def rails(self, blockid, data):
    # first, do rotation
    # Masked to not clobber powered rail on/off info
    # Ascending and flat straight
    if self.rotation == 1:
        if (data & 0b0111) == 0: data = data & 0b1000 | 1
        elif (data & 0b0111) == 1: data = data & 0b1000 | 0
        elif (data & 0b0111) == 2: data = data & 0b1000 | 5
        elif (data & 0b0111) == 3: data = data & 0b1000 | 4
        elif (data & 0b0111) == 4: data = data & 0b1000 | 2
        elif (data & 0b0111) == 5: data = data & 0b1000 | 3
    elif self.rotation == 2:
        if (data & 0b0111) == 2: data = data & 0b1000 | 3
        elif (data & 0b0111) == 3: data = data & 0b1000 | 2
        elif (data & 0b0111) == 4: data = data & 0b1000 | 5
        elif (data & 0b0111) == 5: data = data & 0b1000 | 4
    elif self.rotation == 3:
        if (data & 0b0111) == 0: data = data & 0b1000 | 1
        elif (data & 0b0111) == 1: data = data & 0b1000 | 0
        elif (data & 0b0111) == 2: data = data & 0b1000 | 4
        elif (data & 0b0111) == 3: data = data & 0b1000 | 5
        elif (data & 0b0111) == 4: data = data & 0b1000 | 3
        elif (data & 0b0111) == 5: data = data & 0b1000 | 2
    if blockid == 66: # normal minetrack only
        #Corners
        if self.rotation == 1:
            if data == 6: data = 7
            elif data == 7: data = 8
            elif data == 8: data = 6
            elif data == 9: data = 9
        elif self.rotation == 2:
            if data == 6: data = 8
            elif data == 7: data = 9
            elif data == 8: data = 6
            elif data == 9: data = 7
        elif self.rotation == 3:
            if data == 6: data = 9
            elif data == 7: data = 6
            elif data == 8: data = 8
            elif data == 9: data = 7
    img = Image.new("RGBA", (24,24), self.bgcolor)
    
    if blockid == 27: # powered rail
        if data & 0x8 == 0: # unpowered
            raw_straight = self.load_image_texture("assets/minecraft/textures/block/powered_rail.png")
            raw_corner = self.load_image_texture("assets/minecraft/textures/block/rail_corner.png")    # they don't exist but make the code
                                                # much simplier
        elif data & 0x8 == 0x8: # powered
            raw_straight = self.load_image_texture("assets/minecraft/textures/block/powered_rail_on.png")
            raw_corner = self.load_image_texture("assets/minecraft/textures/block/rail_corner.png")    # leave corners for code simplicity
        # filter the 'powered' bit
        data = data & 0x7
            
    elif blockid == 28: # detector rail
        raw_straight = self.load_image_texture("assets/minecraft/textures/block/detector_rail.png")
        raw_corner = self.load_image_texture("assets/minecraft/textures/block/rail_corner.png")    # leave corners for code simplicity
        
    elif blockid == 66: # normal rail
        raw_straight = self.load_image_texture("assets/minecraft/textures/block/rail.png")
        raw_corner = self.load_image_texture("assets/minecraft/textures/block/rail_corner.png")

    elif blockid == 157: # activator rail
        if data & 0x8 == 0: # unpowered
            raw_straight = self.load_image_texture("assets/minecraft/textures/block/activator_rail.png")
            raw_corner = self.load_image_texture("assets/minecraft/textures/block/rail_corner.png")    # they don't exist but make the code
                                                # much simplier
        elif data & 0x8 == 0x8: # powered
            raw_straight = self.load_image_texture("assets/minecraft/textures/block/activator_rail_on.png")
            raw_corner = self.load_image_texture("assets/minecraft/textures/block/rail_corner.png")    # leave corners for code simplicity
        # filter the 'powered' bit
        data = data & 0x7
        
    ## use transform_image to scale and shear
    if data == 0:
        track = self.transform_image_top(raw_straight)
        alpha_over(img, track, (0,12), track)
    elif data == 6:
        track = self.transform_image_top(raw_corner)
        alpha_over(img, track, (0,12), track)
    elif data == 7:
        track = self.transform_image_top(raw_corner.rotate(270))
        alpha_over(img, track, (0,12), track)
    elif data == 8:
        # flip
        track = self.transform_image_top(raw_corner.transpose(Image.FLIP_TOP_BOTTOM).rotate(90))
        alpha_over(img, track, (0,12), track)
    elif data == 9:
        track = self.transform_image_top(raw_corner.transpose(Image.FLIP_TOP_BOTTOM))
        alpha_over(img, track, (0,12), track)
    elif data == 1:
        track = self.transform_image_top(raw_straight.rotate(90))
        alpha_over(img, track, (0,12), track)
        
    #slopes
    elif data == 2: # slope going up in +x direction
        track = self.transform_image_slope(raw_straight)
        track = track.transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, track, (2,0), track)
        # the 2 pixels move is needed to fit with the adjacent tracks
        
    elif data == 3: # slope going up in -x direction
        # tracks are sprites, in this case we are seeing the "side" of 
        # the sprite, so draw a line to make it beautiful.
        ImageDraw.Draw(img).line([(11,11),(23,17)],fill=(164,164,164))
        # grey from track texture (exterior grey).
        # the track doesn't start from image corners, be carefull drawing the line!
    elif data == 4: # slope going up in -y direction
        track = self.transform_image_slope(raw_straight)
        alpha_over(img, track, (0,0), track)
        
    elif data == 5: # slope going up in +y direction
        # same as "data == 3"
        ImageDraw.Draw(img).line([(1,17),(12,11)],fill=(164,164,164))
        
    return img


# sticky and normal piston body
@material(blockid=[29, 33], data=[0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13],
          transparent=True, solid=True, nospawn=True)
def piston(self, blockid, data):
    # first, rotation
    # Masked to not clobber block head/foot info
    if self.rotation in [1, 2, 3] and (data & 0b111) in [2, 3, 4, 5]:
        rotation_map = {1: {2: 5, 3: 4, 4: 2, 5: 3},
                        2: {2: 3, 3: 2, 4: 5, 5: 4},
                        3: {2: 4, 3: 5, 4: 3, 5: 2}}
        data = (data & 0b1000) | rotation_map[self.rotation][data & 0b111]

    if blockid == 29:  # sticky
        piston_t = self.load_image_texture("assets/minecraft/textures/block/piston_top_sticky.png").copy()
    else:  # normal
        piston_t = self.load_image_texture("assets/minecraft/textures/block/piston_top.png").copy()

    # other textures
    side_t = self.load_image_texture("assets/minecraft/textures/block/piston_side.png").copy()
    back_t = self.load_image_texture("assets/minecraft/textures/block/piston_bottom.png").copy()
    interior_t = self.load_image_texture("assets/minecraft/textures/block/piston_inner.png").copy()

    if data & 0x08 == 0x08:  # pushed out, non full blocks, tricky stuff
        # remove piston texture from piston body
        ImageDraw.Draw(side_t).rectangle((0, 0, 16, 3), outline=(0, 0, 0, 0), fill=(0, 0, 0, 0))

        if data & 0x07 == 0x0:    # down
            side_t = side_t.rotate(180)
            img = self.build_full_block(back_t, None, None, side_t, side_t)
        elif data & 0x07 == 0x1:  # up
            img = self.build_full_block((interior_t, 4), None, None, side_t, side_t)
        elif data & 0x07 == 0x2:  # north
            img = self.build_full_block(side_t, None, None, side_t.rotate(90), back_t)
        elif data & 0x07 == 0x3:  # south
            img = self.build_full_block(side_t.rotate(180), None, None, side_t.rotate(270), None)
            temp = self.transform_image_side(interior_t)
            temp = temp.transpose(Image.FLIP_LEFT_RIGHT)
            alpha_over(img, temp, (9, 4), temp)
        elif data & 0x07 == 0x4:  # west
            img = self.build_full_block(side_t.rotate(90), None, None, None, side_t.rotate(270))
            temp = self.transform_image_side(interior_t)
            alpha_over(img, temp, (3, 4), temp)
        elif data & 0x07 == 0x5:  # east
            img = self.build_full_block(side_t.rotate(270), None, None, back_t, side_t.rotate(90))

    else:  # pushed in, normal full blocks, easy stuff
        if data & 0x07 == 0x0:    # down
            side_t = side_t.rotate(180)
            img = self.build_full_block(back_t, None, None, side_t, side_t)
        elif data & 0x07 == 0x1:  # up
            img = self.build_full_block(piston_t, None, None, side_t, side_t)
        elif data & 0x07 == 0x2:  # north
            img = self.build_full_block(side_t, None, None, side_t.rotate(90), back_t)
        elif data & 0x07 == 0x3:  # south
            img = self.build_full_block(side_t.rotate(180), None, None, side_t.rotate(270), piston_t)
        elif data & 0x07 == 0x4:  # west
            img = self.build_full_block(side_t.rotate(90), None, None, piston_t, side_t.rotate(270))
        elif data & 0x07 == 0x5:  # east
            img = self.build_full_block(side_t.rotate(270), None, None, back_t, side_t.rotate(90))

    return img


# sticky and normal piston shaft
@material(blockid=34, data=[0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13], transparent=True, nospawn=True)
def piston_extension(self, blockid, data):
    # first, rotation
    # Masked to not clobber block head/foot info
    if self.rotation in [1, 2, 3] and (data & 0b111) in [2, 3, 4, 5]:
        rotation_map = {1: {2: 5, 3: 4, 4: 2, 5: 3},
                        2: {2: 3, 3: 2, 4: 5, 5: 4},
                        3: {2: 4, 3: 5, 4: 3, 5: 2}}
        data = (data & 0b1000) | rotation_map[self.rotation][data & 0b111]

    if data & 0x8 == 0x8:  # sticky
        piston_t = self.load_image_texture("assets/minecraft/textures/block/piston_top_sticky.png").copy()
    else:  # normal
        piston_t = self.load_image_texture("assets/minecraft/textures/block/piston_top.png").copy()

    # other textures
    side_t = self.load_image_texture("assets/minecraft/textures/block/piston_side.png").copy()
    back_t = self.load_image_texture("assets/minecraft/textures/block/piston_top.png").copy()
    # crop piston body
    ImageDraw.Draw(side_t).rectangle((0, 4, 16, 16), outline=(0, 0, 0, 0), fill=(0, 0, 0, 0))

    # generate the horizontal piston extension stick
    h_stick = Image.new("RGBA", (24, 24), self.bgcolor)
    temp = self.transform_image_side(side_t)
    alpha_over(h_stick, temp, (1, 7), temp)
    temp = self.transform_image_top(side_t.rotate(90))
    alpha_over(h_stick, temp, (1, 1), temp)
    # Darken it
    sidealpha = h_stick.split()[3]
    h_stick = ImageEnhance.Brightness(h_stick).enhance(0.85)
    h_stick.putalpha(sidealpha)

    # generate the vertical piston extension stick
    v_stick = Image.new("RGBA", (24, 24), self.bgcolor)
    temp = self.transform_image_side(side_t.rotate(90))
    alpha_over(v_stick, temp, (12, 6), temp)
    temp = temp.transpose(Image.FLIP_LEFT_RIGHT)
    alpha_over(v_stick, temp, (1, 6), temp)
    # Darken it
    sidealpha = v_stick.split()[3]
    v_stick = ImageEnhance.Brightness(v_stick).enhance(0.85)
    v_stick.putalpha(sidealpha)

    # Piston orientation is stored in the 3 first bits
    if data & 0x07 == 0x0:    # down
        side_t = side_t.rotate(180)
        img = self.build_full_block((back_t, 12), None, None, side_t, side_t)
        alpha_over(img, v_stick, (0, -3), v_stick)
    elif data & 0x07 == 0x1:  # up
        img = Image.new("RGBA", (24, 24), self.bgcolor)
        img2 = self.build_full_block(piston_t, None, None, side_t, side_t)
        alpha_over(img, v_stick, (0, 4), v_stick)
        alpha_over(img, img2, (0, 0), img2)
    elif data & 0x07 == 0x2:  # north
        img = self.build_full_block(side_t, None, None, side_t.rotate(90), None)
        temp = self.transform_image_side(back_t).transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, temp, (2, 2), temp)
        alpha_over(img, h_stick, (6, 3), h_stick)
    elif data & 0x07 == 0x3:  # south
        img = Image.new("RGBA", (24, 24), self.bgcolor)
        img2 = self.build_full_block(side_t.rotate(180), None, None, side_t.rotate(270), piston_t)
        alpha_over(img, h_stick, (0, 0), h_stick)
        alpha_over(img, img2, (0, 0), img2)
    elif data & 0x07 == 0x4:  # west
        img = self.build_full_block(side_t.rotate(90), None, None, piston_t, side_t.rotate(270))
        h_stick = h_stick.transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, h_stick, (0, 0), h_stick)
    elif data & 0x07 == 0x5:  # east
        img = Image.new("RGBA", (24, 24), self.bgcolor)
        img2 = self.build_full_block(side_t.rotate(270), None, None, None, side_t.rotate(90))
        h_stick = h_stick.transpose(Image.FLIP_LEFT_RIGHT)
        temp = self.transform_image_side(back_t)
        alpha_over(img2, temp, (10, 2), temp)
        alpha_over(img, img2, (0, 0), img2)
        alpha_over(img, h_stick, (-3, 2), h_stick)

    return img


# cobweb
sprite(blockid=30, imagename="assets/minecraft/textures/block/cobweb.png", nospawn=True)

@material(blockid=31, data=list(range(3)), transparent=True)
def tall_grass(self, blockid, data):
    if data == 0: # dead shrub
        texture = self.load_image_texture("assets/minecraft/textures/block/dead_bush.png")
    elif data == 1: # tall grass
        texture = self.load_image_texture("assets/minecraft/textures/block/grass.png")
    elif data == 2: # fern
        texture = self.load_image_texture("assets/minecraft/textures/block/fern.png")
    
    return self.build_billboard(texture)

# dead bush
billboard(blockid=32, imagename="assets/minecraft/textures/block/dead_bush.png")

# dandelion
sprite(blockid=37, imagename="assets/minecraft/textures/block/dandelion.png")

# flowers
@material(blockid=38, data=list(range(13)), transparent=True)
def flower(self, blockid, data):
    flower_map = ["poppy", "blue_orchid", "allium", "azure_bluet", "red_tulip", "orange_tulip",
                  "white_tulip", "pink_tulip", "oxeye_daisy", "dandelion", "wither_rose",
                  "cornflower", "lily_of_the_valley"]
    texture = self.load_image_texture("assets/minecraft/textures/block/%s.png" % flower_map[data])
    return self.build_billboard(texture)

# brown mushroom
sprite(blockid=39, imagename="assets/minecraft/textures/block/brown_mushroom.png")
# red mushroom
sprite(blockid=40, imagename="assets/minecraft/textures/block/red_mushroom.png")
# warped fungus
sprite(blockid=1016, imagename="assets/minecraft/textures/block/warped_fungus.png")
# crimson fungus
sprite(blockid=1017, imagename="assets/minecraft/textures/block/crimson_fungus.png")
# warped roots
sprite(blockid=1018, imagename="assets/minecraft/textures/block/warped_roots.png")
# crimson roots
sprite(blockid=1019, imagename="assets/minecraft/textures/block/crimson_roots.png")

# mineral overlay
# block of gold
solidmodelblock(blockid=41, name="gold_block")
# block of iron
solidmodelblock(blockid=42, name="iron_block")

# double slabs and slabs
# these wooden slabs are unobtainable without cheating, they are still
# here because lots of pre-1.3 worlds use this blocks, add prismarine slabs
@material(blockid=[43, 44, 181, 182, 204, 205, 1124, 1789] + list(range(11340, 11359)) +
          list(range(1027, 1030)) + list(range(1072, 1080)) + list(range(1103, 1107)),
          data=list(range(16)),
          transparent=[44, 182, 205, 1124, 1789] + list(range(11340, 11359)) + list(range(1027, 1030)) +
          list(range(1072, 1080)) + list(range(1103, 1107)), solid=True)
def slabs(self, blockid, data):
    if blockid == 44 or blockid == 182: 
        texture = data & 7
    else: # data > 8 are special double slabs
        texture = data

    if blockid == 44 or blockid == 43:
        if texture== 0: # stone slab
            top = self.load_image_texture("assets/minecraft/textures/block/stone.png")
            side = self.load_image_texture("assets/minecraft/textures/block/stone.png")
        elif texture== 1: # sandstone slab
            top = self.load_image_texture("assets/minecraft/textures/block/sandstone_top.png")
            side = self.load_image_texture("assets/minecraft/textures/block/sandstone.png")
        elif texture== 2: # wooden slab
            top = side = self.load_image_texture("assets/minecraft/textures/block/oak_planks.png")
        elif texture== 3: # cobblestone slab
            top = side = self.load_image_texture("assets/minecraft/textures/block/cobblestone.png")
        elif texture== 4: # brick
            top = side = self.load_image_texture("assets/minecraft/textures/block/bricks.png")
        elif texture== 5: # stone brick
            top = side = self.load_image_texture("assets/minecraft/textures/block/stone_bricks.png")
        elif texture== 6: # nether brick slab
            top = side = self.load_image_texture("assets/minecraft/textures/block/nether_bricks.png")
        elif texture== 7: #quartz        
            top = side = self.load_image_texture("assets/minecraft/textures/block/quartz_block_side.png")
        elif texture== 8: # special stone double slab with top texture only
            top = side = self.load_image_texture("assets/minecraft/textures/block/smooth_stone.png")
        elif texture== 9: # special sandstone double slab with top texture only
            top = side = self.load_image_texture("assets/minecraft/textures/block/sandstone_top.png")
        else:
            return None

    elif blockid == 182: # single red sandstone slab
        if texture == 0:
            top = self.load_image_texture("assets/minecraft/textures/block/red_sandstone_top.png")
            side = self.load_image_texture("assets/minecraft/textures/block/red_sandstone.png")
        else:
            return None

    elif blockid == 181: # double red sandstone slab
        if texture == 0: # red sandstone
            top = self.load_image_texture("assets/minecraft/textures/block/red_sandstone_top.png")
            side = self.load_image_texture("assets/minecraft/textures/block/red_sandstone.png")
        elif texture == 8: # 'full' red sandstone (smooth)
            top = side = self.load_image_texture("assets/minecraft/textures/block/red_sandstone_top.png");
        else:
            return None
    elif blockid == 204 or blockid == 205: # purpur slab (single=205 double=204)
        top = side = self.load_image_texture("assets/minecraft/textures/block/purpur_block.png");

    elif blockid == 11340: # prismarine slabs
        top = side = self.load_image_texture("assets/minecraft/textures/block/prismarine.png").copy()
    elif blockid == 11341: # dark prismarine slabs
        top = side  = self.load_image_texture("assets/minecraft/textures/block/dark_prismarine.png").copy()
    elif blockid == 11342: #  prismarine brick slabs
        top = side  = self.load_image_texture("assets/minecraft/textures/block/prismarine_bricks.png").copy()
    elif blockid == 11343: #  andesite slabs
        top = side  = self.load_image_texture("assets/minecraft/textures/block/andesite.png").copy()
    elif blockid == 11344: #  diorite slabs
        top = side  = self.load_image_texture("assets/minecraft/textures/block/diorite.png").copy()
    elif blockid == 11345: #  granite slabs
        top = side  = self.load_image_texture("assets/minecraft/textures/block/granite.png").copy()
    elif blockid == 11346: #  polished andesite slabs
        top = side  = self.load_image_texture("assets/minecraft/textures/block/polished_andesite.png").copy()
    elif blockid == 11347: #  polished diorite slabs
        top = side  = self.load_image_texture("assets/minecraft/textures/block/polished_diorite.png").copy()
    elif blockid == 11348: #  polished granite slabs
        top = side  = self.load_image_texture("assets/minecraft/textures/block/polished_granite.png").copy()
    elif blockid == 11349: #  red nether brick slab
        top = side  = self.load_image_texture("assets/minecraft/textures/block/red_nether_bricks.png").copy()
    elif blockid == 11350: #  smooth sandstone slab
        top = side  = self.load_image_texture("assets/minecraft/textures/block/sandstone_top.png").copy()
    elif blockid == 11351: #  cut sandstone slab
        top = side  = self.load_image_texture("assets/minecraft/textures/block/cut_sandstone.png").copy()
    elif blockid == 11352: #  smooth red sandstone slab
        top = side  = self.load_image_texture("assets/minecraft/textures/block/red_sandstone_top.png").copy()
    elif blockid == 11353: #  cut red sandstone slab
        top = side  = self.load_image_texture("assets/minecraft/textures/block/cut_red_sandstone.png").copy()
    elif blockid == 11354: #  end_stone_brick_slab
        top = side  = self.load_image_texture("assets/minecraft/textures/block/end_stone_bricks.png").copy()
    elif blockid == 11355: #  mossy_cobblestone_slab
        top = side  = self.load_image_texture("assets/minecraft/textures/block/mossy_cobblestone.png").copy()
    elif blockid == 11356: #  mossy_stone_brick_slab
        top = side  = self.load_image_texture("assets/minecraft/textures/block/mossy_stone_bricks.png").copy()
    elif blockid == 11357: #  smooth_quartz_slab
        top = side  = self.load_image_texture("assets/minecraft/textures/block/quartz_block_bottom.png").copy()
    elif blockid == 11358: #  smooth_stone_slab
        top  = self.load_image_texture("assets/minecraft/textures/block/smooth_stone.png").copy()
        side = self.load_image_texture("assets/minecraft/textures/block/smooth_stone_slab_side.png").copy()
    elif blockid == 1027: #  blackstone_slab
        top = side  = self.load_image_texture("assets/minecraft/textures/block/blackstone.png").copy()
    elif blockid == 1028: #  polished_blackstone_slab
        top = side  = self.load_image_texture("assets/minecraft/textures/block/polished_blackstone.png").copy()
    elif blockid == 1029: #  polished_blackstone_brick_slab
        top = side  = self.load_image_texture("assets/minecraft/textures/block/polished_blackstone_bricks.png").copy()
    elif blockid in range(1072, 1080):
        copper_tex = {
            1072: "assets/minecraft/textures/block/cut_copper.png",
            1076: "assets/minecraft/textures/block/cut_copper.png",
            1073: "assets/minecraft/textures/block/exposed_cut_copper.png",
            1077: "assets/minecraft/textures/block/exposed_cut_copper.png",
            1074: "assets/minecraft/textures/block/weathered_cut_copper.png",
            1078: "assets/minecraft/textures/block/weathered_cut_copper.png",
            1075: "assets/minecraft/textures/block/oxidized_cut_copper.png",
            1079: "assets/minecraft/textures/block/oxidized_cut_copper.png",
        }
        top = side = self.load_image_texture(copper_tex[blockid]).copy()
    elif blockid in range(1103, 1107):
        deepslate_tex = {
            1103: "assets/minecraft/textures/block/cobbled_deepslate.png",
            1104: "assets/minecraft/textures/block/polished_deepslate.png",
            1105: "assets/minecraft/textures/block/deepslate_bricks.png",
            1106: "assets/minecraft/textures/block/deepslate_tiles.png",
        }
        top = side = self.load_image_texture(deepslate_tex[blockid]).copy()
    elif blockid == 1124:
        top = side = self.load_image_texture("assets/minecraft/textures/block/mud_bricks.png").copy()
    elif blockid == 1789:
        top = side = self.load_image_texture("assets/minecraft/textures/block/mangrove_planks.png").copy()

    if blockid == 43 or blockid == 181 or blockid == 204: # double slab
        return self.build_block(top, side)
    
    return self.build_slab_block(top, side, data & 8 == 8)

# TNT
solidmodelblock(blockid=46, name="tnt", nospawn=True)
# mineral overlay
# moss stone
solidmodelblock(blockid=48, name="mossy_cobblestone")

# torch, redstone torch (off), redstone torch(on), soul_torch
@material(blockid=[50, 75, 76, 1039], data=[1, 2, 3, 4, 5], transparent=True)
def torches(self, blockid, data):
    # first, rotations
    if self.rotation == 1:
        if data == 1: data = 3
        elif data == 2: data = 4
        elif data == 3: data = 2
        elif data == 4: data = 1
    elif self.rotation == 2:
        if data == 1: data = 2
        elif data == 2: data = 1
        elif data == 3: data = 4
        elif data == 4: data = 3
    elif self.rotation == 3:
        if data == 1: data = 4
        elif data == 2: data = 3
        elif data == 3: data = 1
        elif data == 4: data = 2
    
    # choose the proper texture
    if blockid == 50: # torch
        small = self.load_image_texture("assets/minecraft/textures/block/torch.png")
    elif blockid == 75: # off redstone torch
        small = self.load_image_texture("assets/minecraft/textures/block/redstone_torch_off.png")
    elif blockid == 76: # on redstone torch
        small = self.load_image_texture("assets/minecraft/textures/block/redstone_torch.png")
    elif blockid == 1039: # soul torch
        small= self.load_image_texture("assets/minecraft/textures/block/soul_torch.png")
    # compose a torch bigger than the normal
    # (better for doing transformations)
    torch = Image.new("RGBA", (16,16), self.bgcolor)
    alpha_over(torch,small,(-4,-3))
    alpha_over(torch,small,(-5,-2))
    alpha_over(torch,small,(-3,-2))
    
    # angle of inclination of the texture
    rotation = 15
    
    if data == 1: # pointing south
        torch = torch.rotate(-rotation, Image.NEAREST) # nearest filter is more nitid.
        img = self.build_full_block(None, None, None, torch, None, None)
        
    elif data == 2: # pointing north
        torch = torch.rotate(rotation, Image.NEAREST)
        img = self.build_full_block(None, None, torch, None, None, None)
        
    elif data == 3: # pointing west
        torch = torch.rotate(rotation, Image.NEAREST)
        img = self.build_full_block(None, torch, None, None, None, None)
        
    elif data == 4: # pointing east
        torch = torch.rotate(-rotation, Image.NEAREST)
        img = self.build_full_block(None, None, None, None, torch, None)
        
    elif data == 5: # standing on the floor
        # compose a "3d torch".
        img = Image.new("RGBA", (24,24), self.bgcolor)
        
        small_crop = small.crop((2,2,14,14))
        slice = small_crop.copy()
        ImageDraw.Draw(slice).rectangle((6,0,12,12),outline=(0,0,0,0),fill=(0,0,0,0))
        ImageDraw.Draw(slice).rectangle((0,0,4,12),outline=(0,0,0,0),fill=(0,0,0,0))
        
        alpha_over(img, slice, (7,5))
        alpha_over(img, small_crop, (6,6))
        alpha_over(img, small_crop, (7,6))
        alpha_over(img, slice, (7,7))
        
    return img

# lantern
@material(blockid=[11373, 1038], data=[0, 1], transparent=True)
def lantern(self, blockid, data):
    # get the  multipart texture of the lantern
    if blockid == 11373:
        inputtexture = self.load_image_texture("assets/minecraft/textures/block/lantern.png")
    if blockid == 1038:
        inputtexture = self.load_image_texture("assets/minecraft/textures/block/soul_lantern.png")


    # # now create a textures, using the parts defined in lantern.json

    # JSON data for sides
    # from": [ 5,  1,  5 ],
    #  "to": [11,  8, 11 ],
    # { "uv": [ 0, 2, 6,  9 ], "texture": "#all" }

    side_crop = inputtexture.crop((0, 2, 6, 9))
    side_slice = side_crop.copy()
    side_texture = Image.new("RGBA", (16, 16), self.bgcolor)
    side_texture.paste(side_slice,(5, 8))

    # JSON data for top
    # { "uv": [  0, 9,  6, 15 ], "texture": "#all" }
    top_crop = inputtexture.crop((0, 9, 6, 15))
    top_slice = top_crop.copy()
    top_texture = Image.new("RGBA", (16, 16), self.bgcolor)
    top_texture.paste(top_slice,(5, 5))

    # mimic parts of build_full_block, to get an object smaller than a block 
    # build_full_block(self, top, side1, side2, side3, side4, bottom=None):
    # a non transparent block uses top, side 3 and side 4.
    img = Image.new("RGBA", (24, 24), self.bgcolor)
    # prepare the side textures
    # side3
    side3 = self.transform_image_side(side_texture)
    # Darken this side
    sidealpha = side3.split()[3]
    side3 = ImageEnhance.Brightness(side3).enhance(0.9)
    side3.putalpha(sidealpha)
    # place the transformed texture
    hangoff = 0
    if data == 1:
        hangoff = 8
    xoff = 4
    yoff =- hangoff
    alpha_over(img, side3, (xoff+0, yoff+6), side3)
    # side4
    side4 = self.transform_image_side(side_texture)
    side4 = side4.transpose(Image.FLIP_LEFT_RIGHT)
    # Darken this side
    sidealpha = side4.split()[3]
    side4 = ImageEnhance.Brightness(side4).enhance(0.8)
    side4.putalpha(sidealpha)
    alpha_over(img, side4, (12-xoff, yoff+6), side4)
    # top
    top = self.transform_image_top(top_texture)
    alpha_over(img, top, (0, 8-hangoff), top)
    return img

# bamboo
@material(blockid=11416, transparent=True)
def bamboo(self, blockid, data):
    # get the  multipart texture of the lantern
    inputtexture = self.load_image_texture("assets/minecraft/textures/block/bamboo_stalk.png")

    # # now create a textures, using the parts defined in bamboo1_age0.json
        # {   "from": [ 7, 0, 7 ],
        #     "to": [ 9, 16, 9 ],
        #     "faces": {
        #         "down":  { "uv": [ 13, 4, 15, 6 ], "texture": "#all", "cullface": "down" },
        #         "up":    { "uv": [ 13, 0, 15, 2], "texture": "#all", "cullface": "up" },
        #         "north": { "uv": [ 0, 0, 2, 16 ], "texture": "#all" },
        #         "south": { "uv": [ 0, 0, 2, 16 ], "texture": "#all" },
        #         "west":  { "uv": [  0, 0, 2, 16 ], "texture": "#all" },
        #         "east":  { "uv": [  0, 0, 2, 16 ], "texture": "#all" }
        #     }
        # }

    side_crop = inputtexture.crop((0, 0, 3, 16))
    side_slice = side_crop.copy()
    side_texture = Image.new("RGBA", (16, 16), self.bgcolor)
    side_texture.paste(side_slice,(0, 0))

    # JSON data for top
    # "up":    { "uv": [ 13, 0, 15, 2], "texture": "#all", "cullface": "up" },
    top_crop = inputtexture.crop((13, 0, 16, 3))
    top_slice = top_crop.copy()
    top_texture = Image.new("RGBA", (16, 16), self.bgcolor)
    top_texture.paste(top_slice,(5, 5))

    # mimic parts of build_full_block, to get an object smaller than a block 
    # build_full_block(self, top, side1, side2, side3, side4, bottom=None):
    # a non transparent block uses top, side 3 and side 4.
    img = Image.new("RGBA", (24, 24), self.bgcolor)
    # prepare the side textures
    # side3
    side3 = self.transform_image_side(side_texture)
    # Darken this side
    sidealpha = side3.split()[3]
    side3 = ImageEnhance.Brightness(side3).enhance(0.9)
    side3.putalpha(sidealpha)
    # place the transformed texture
    xoff = 3
    yoff = 0
    alpha_over(img, side3, (4+xoff, yoff), side3)
    # side4
    side4 = self.transform_image_side(side_texture)
    side4 = side4.transpose(Image.FLIP_LEFT_RIGHT)
    # Darken this side
    sidealpha = side4.split()[3]
    side4 = ImageEnhance.Brightness(side4).enhance(0.8)
    side4.putalpha(sidealpha)
    alpha_over(img, side4, (-4+xoff, yoff), side4)
    # top
    top = self.transform_image_top(top_texture)
    alpha_over(img, top, (-4+xoff, -5), top)
    return img

# composter
@material(blockid=11417, data=list(range(9)), transparent=True)
def composter(self, blockid, data):
    side = self.load_image_texture("assets/minecraft/textures/block/composter_side.png")
    top = self.load_image_texture("assets/minecraft/textures/block/composter_top.png")
    # bottom = self.load_image_texture("assets/minecraft/textures/block/composter_bottom.png")

    if data == 0:  # empty
        return self.build_full_block(top, side, side, side, side)

    if data == 8:
        compost = self.transform_image_top(
            self.load_image_texture("assets/minecraft/textures/block/composter_ready.png"))
    else:
        compost = self.transform_image_top(
            self.load_image_texture("assets/minecraft/textures/block/composter_compost.png"))

    nudge = {1: (0, 9), 2: (0, 8), 3: (0, 7), 4: (0, 6), 5: (0, 4), 6: (0, 2), 7: (0, 0), 8: (0, 0)}

    img = self.build_full_block(None, side, side, None, None)
    alpha_over(img, compost, nudge[data], compost)
    img2 = self.build_full_block(top, None, None, side, side)
    alpha_over(img, img2, (0, 0), img2)
    return img

# fire and soul_fire
@material(blockid=[51, 1040], transparent=True)
def fire(self, blockid, data):
    if blockid == 51:
        return self.build_block_from_model('fire_floor0')
    else:
        return self.build_block_from_model('soul_fire_floor0')

# monster spawner
modelblock(blockid=52, name="spawner", solid=True, transparent=True)

# wooden, cobblestone, red brick, stone brick, netherbrick, sandstone, spruce, birch,
# jungle, quartz, red sandstone, purpur_stairs, crimson_stairs, warped_stairs, (dark) prismarine,
# mossy brick and mossy cobblestone, stone smooth_quartz
# polished_granite polished_andesite polished_diorite granite diorite andesite end_stone_bricks red_nether_brick stairs
# smooth_red_sandstone blackstone polished_blackstone polished_blackstone_brick
# also all the copper variants
# also all deepslate variants
@material(blockid=[53, 67, 108, 109, 114, 128, 134, 135, 136, 156, 163, 164, 180, 203, 509, 510,
                   11337, 11338, 11339, 11370, 11371, 11374, 11375, 11376, 11377, 11378, 11379,
                   11380, 11381, 11382, 11383, 11384, 11415, 1030, 1031, 1032, 1064, 1065, 1066,
                   1067, 1068, 1069, 1070, 1071, 1099, 1100, 1101, 1102],
          data=list(range(128)), transparent=True, solid=True, nospawn=True)
def stairs(self, blockid, data):
    # preserve the upside-down bit
    upside_down = data & 0x4

    # find solid quarters within the top or bottom half of the block
    #                   NW           NE           SE           SW
    quarters = [data & 0x8, data & 0x10, data & 0x20, data & 0x40]

    # rotate the quarters so we can pretend northdirection is always upper-left
    numpy.roll(quarters, [0,1,3,2][self.rotation])
    nw,ne,se,sw = quarters

    stair_id_to_tex = {
        53: "assets/minecraft/textures/block/oak_planks.png",
        67: "assets/minecraft/textures/block/cobblestone.png",
        108: "assets/minecraft/textures/block/bricks.png",
        109: "assets/minecraft/textures/block/stone_bricks.png",
        114: "assets/minecraft/textures/block/nether_bricks.png",
        128: "assets/minecraft/textures/block/sandstone.png",
        134: "assets/minecraft/textures/block/spruce_planks.png",
        135: "assets/minecraft/textures/block/birch_planks.png",
        136: "assets/minecraft/textures/block/jungle_planks.png",
        156: "assets/minecraft/textures/block/quartz_block_side.png",
        163: "assets/minecraft/textures/block/acacia_planks.png",
        164: "assets/minecraft/textures/block/dark_oak_planks.png",
        180: "assets/minecraft/textures/block/red_sandstone.png",
        203: "assets/minecraft/textures/block/purpur_block.png",
        509: "assets/minecraft/textures/block/crimson_planks.png",
        510: "assets/minecraft/textures/block/warped_planks.png",
        11337: "assets/minecraft/textures/block/prismarine.png",
        11338: "assets/minecraft/textures/block/dark_prismarine.png",
        11339: "assets/minecraft/textures/block/prismarine_bricks.png",
        11370: "assets/minecraft/textures/block/mossy_stone_bricks.png",
        11371: "assets/minecraft/textures/block/mossy_cobblestone.png",
        11374: "assets/minecraft/textures/block/sandstone_top.png",
        11375: "assets/minecraft/textures/block/quartz_block_side.png",
        11376: "assets/minecraft/textures/block/polished_granite.png",
        11377: "assets/minecraft/textures/block/polished_diorite.png",
        11378: "assets/minecraft/textures/block/polished_andesite.png",
        11379: "assets/minecraft/textures/block/stone.png",
        11380: "assets/minecraft/textures/block/granite.png",
        11381: "assets/minecraft/textures/block/diorite.png",
        11382: "assets/minecraft/textures/block/andesite.png",
        11383: "assets/minecraft/textures/block/end_stone_bricks.png",
        11384: "assets/minecraft/textures/block/red_nether_bricks.png",
        11415: "assets/minecraft/textures/block/red_sandstone_top.png",
        1030: "assets/minecraft/textures/block/blackstone.png",
        1031: "assets/minecraft/textures/block/polished_blackstone.png",
        1032: "assets/minecraft/textures/block/polished_blackstone_bricks.png",
        # Cut copper stairs
        1064: "assets/minecraft/textures/block/cut_copper.png",
        1065: "assets/minecraft/textures/block/exposed_cut_copper.png",
        1066: "assets/minecraft/textures/block/weathered_cut_copper.png",
        1067: "assets/minecraft/textures/block/oxidized_cut_copper.png",
        # Waxed cut copper stairs
        1068: "assets/minecraft/textures/block/cut_copper.png",
        1069: "assets/minecraft/textures/block/exposed_cut_copper.png",
        1070: "assets/minecraft/textures/block/weathered_cut_copper.png",
        1071: "assets/minecraft/textures/block/oxidized_cut_copper.png",
        # Deepslate
        1099: "assets/minecraft/textures/block/cobbled_deepslate.png",
        1100: "assets/minecraft/textures/block/polished_deepslate.png",
        1101: "assets/minecraft/textures/block/deepslate_bricks.png",
        1102: "assets/minecraft/textures/block/deepslate_tiles.png",
    }

    texture = self.load_image_texture(stair_id_to_tex[blockid]).copy()

    outside_l = texture.copy()
    outside_r = texture.copy()
    inside_l = texture.copy()
    inside_r = texture.copy()

    # sandstone, red sandstone, and quartz stairs have special top texture
    special_tops = {
        128: "assets/minecraft/textures/block/sandstone_top.png",
        156: "assets/minecraft/textures/block/quartz_block_top.png",
        180: "assets/minecraft/textures/block/red_sandstone_top.png",
        11375: "assets/minecraft/textures/block/quartz_block_top.png",
    }

    if blockid in special_tops:
        texture = self.load_image_texture(special_tops[blockid]).copy()
 

    slab_top = texture.copy()

    push = 8 if upside_down else 0

    def rect(tex,coords):
        ImageDraw.Draw(tex).rectangle(coords,outline=(0,0,0,0),fill=(0,0,0,0))

    # cut out top or bottom half from inner surfaces
    rect(inside_l, (0,8-push,15,15-push))
    rect(inside_r, (0,8-push,15,15-push))

    # cut out missing or obstructed quarters from each surface
    if not nw:
        rect(outside_l, (0,push,7,7+push))
        rect(texture, (0,0,7,7))
    if not nw or sw:
        rect(inside_r, (8,push,15,7+push)) # will be flipped
    if not ne:
        rect(texture, (8,0,15,7))
    if not ne or nw:
        rect(inside_l, (0,push,7,7+push))
    if not ne or se:
        rect(inside_r, (0,push,7,7+push)) # will be flipped
    if not se:
        rect(outside_r, (0,push,7,7+push)) # will be flipped
        rect(texture, (8,8,15,15))
    if not se or sw:
        rect(inside_l, (8,push,15,7+push))
    if not sw:
        rect(outside_l, (8,push,15,7+push))
        rect(outside_r, (8,push,15,7+push)) # will be flipped
        rect(texture, (0,8,7,15))

    img = Image.new("RGBA", (24,24), self.bgcolor)

    if upside_down:
        # top should have no cut-outs after all
        texture = slab_top
    else:
        # render the slab-level surface
        slab_top = self.transform_image_top(slab_top)
        alpha_over(img, slab_top, (0,6))

    # render inner left surface
    inside_l = self.transform_image_side(inside_l)
    # Darken the vertical part of the second step
    sidealpha = inside_l.split()[3]
    # darken it a bit more than usual, looks better
    inside_l = ImageEnhance.Brightness(inside_l).enhance(0.8)
    inside_l.putalpha(sidealpha)
    alpha_over(img, inside_l, (6,3))

    # render inner right surface
    inside_r = self.transform_image_side(inside_r).transpose(Image.FLIP_LEFT_RIGHT)
    # Darken the vertical part of the second step
    sidealpha = inside_r.split()[3]
    # darken it a bit more than usual, looks better
    inside_r = ImageEnhance.Brightness(inside_r).enhance(0.7)
    inside_r.putalpha(sidealpha)
    alpha_over(img, inside_r, (6,3))

    # render outer surfaces
    alpha_over(img, self.build_full_block(texture, None, None, outside_l, outside_r))

    return img

# normal, locked (used in april's fool day), ender and trapped chest
# NOTE:  locked chest used to be id95 (which is now stained glass)
@material(blockid=[54, 130, 146], data=list(range(30)), transparent = True)
def chests(self, blockid, data):
    # the first 3 bits are the orientation as stored in minecraft, 
    # bits 0x8 and 0x10 indicate which half of the double chest is it.

    # first, do the rotation if needed
    orientation_data = data & 7
    if self.rotation == 1:
        if orientation_data == 2: data = 5 | (data & 24)
        elif orientation_data == 3: data = 4 | (data & 24)
        elif orientation_data == 4: data = 2 | (data & 24)
        elif orientation_data == 5: data = 3 | (data & 24)
    elif self.rotation == 2:
        if orientation_data == 2: data = 3 | (data & 24)
        elif orientation_data == 3: data = 2 | (data & 24)
        elif orientation_data == 4: data = 5 | (data & 24)
        elif orientation_data == 5: data = 4 | (data & 24)
    elif self.rotation == 3:
        if orientation_data == 2: data = 4 | (data & 24)
        elif orientation_data == 3: data = 5 | (data & 24)
        elif orientation_data == 4: data = 3 | (data & 24)
        elif orientation_data == 5: data = 2 | (data & 24)
    
    if blockid == 130 and not data in [2, 3, 4, 5]: return None
        # iterate.c will only return the ancil data (without pseudo 
        # ancil data) for locked and ender chests, so only 
        # ancilData = 2,3,4,5 are used for this blockids
    
    if data & 24 == 0:
        if blockid == 130: t = self.load_image("assets/minecraft/textures/entity/chest/ender.png")
        else:
            try:
                t = self.load_image("assets/minecraft/textures/entity/chest/normal.png")
            except (TextureException, IOError):
                t = self.load_image("assets/minecraft/textures/entity/chest/chest.png")

        t = ImageOps.flip(t) # for some reason the 1.15 images are upside down

        # the textures is no longer in terrain.png, get it from
        # item/chest.png and get by cropping all the needed stuff
        if t.size != (64, 64): t = t.resize((64, 64), Image.ANTIALIAS)
        # top
        top = t.crop((28, 50, 42, 64))
        top.load() # every crop need a load, crop is a lazy operation
                   # see PIL manual
        img = Image.new("RGBA", (16, 16), self.bgcolor)
        alpha_over(img, top, (1, 1))
        top = img
        # front
        front_top = t.crop((42, 45, 56, 50))
        front_top.load()
        front_bottom = t.crop((42, 21, 56, 31))
        front_bottom.load()
        front_lock = t.crop((1, 59, 3, 63))
        front_lock.load()
        front = Image.new("RGBA", (16, 16), self.bgcolor)
        alpha_over(front, front_top, (1, 1))
        alpha_over(front, front_bottom, (1, 5))
        alpha_over(front, front_lock, (7, 3))
        # left side
        # left side, right side, and back are essentially the same for
        # the default texture, we take it anyway just in case other
        # textures make use of it.
        side_l_top = t.crop((14, 45, 28, 50))
        side_l_top.load()
        side_l_bottom = t.crop((14, 21, 28, 31))
        side_l_bottom.load()
        side_l = Image.new("RGBA", (16, 16), self.bgcolor)
        alpha_over(side_l, side_l_top, (1, 1))
        alpha_over(side_l, side_l_bottom, (1, 5))
        # right side
        side_r_top = t.crop((28, 45, 42, 50))
        side_r_top.load()
        side_r_bottom = t.crop((28, 21, 42, 31))
        side_r_bottom.load()
        side_r = Image.new("RGBA", (16, 16), self.bgcolor)
        alpha_over(side_r, side_r_top, (1, 1))
        alpha_over(side_r, side_r_bottom, (1, 5))
        # back
        back_top = t.crop((0, 45, 14, 50))
        back_top.load()
        back_bottom = t.crop((0, 21, 14, 31))
        back_bottom.load()
        back = Image.new("RGBA", (16, 16), self.bgcolor)
        alpha_over(back, back_top, (1, 1))
        alpha_over(back, back_bottom, (1, 5))

    else:
        # large chest
        # the textures is no longer in terrain.png, get it from 
        # item/chest.png and get all the needed stuff
        t_left = self.load_image("assets/minecraft/textures/entity/chest/normal_left.png")
        t_right = self.load_image("assets/minecraft/textures/entity/chest/normal_right.png")
        # for some reason the 1.15 images are upside down
        t_left = ImageOps.flip(t_left)
        t_right = ImageOps.flip(t_right)

        # Top
        top_left = t_right.crop((29, 50, 44, 64))
        top_left.load()
        top_right = t_left.crop((29, 50, 44, 64))
        top_right.load()

        top = Image.new("RGBA", (32, 16), self.bgcolor)
        alpha_over(top,top_left, (1, 1))
        alpha_over(top,top_right, (16, 1))

        # Front
        front_top_left = t_left.crop((43, 45, 58, 50))
        front_top_left.load()
        front_top_right = t_right.crop((43, 45, 58, 50))
        front_top_right.load()

        front_bottom_left = t_left.crop((43, 21, 58, 31))
        front_bottom_left.load()
        front_bottom_right = t_right.crop((43, 21, 58, 31))
        front_bottom_right.load()

        front_lock = t_left.crop((1, 59, 3, 63))
        front_lock.load()

        front = Image.new("RGBA", (32, 16), self.bgcolor)
        alpha_over(front, front_top_left, (1, 1))
        alpha_over(front, front_top_right, (16, 1))
        alpha_over(front, front_bottom_left, (1, 5))
        alpha_over(front, front_bottom_right, (16, 5))
        alpha_over(front, front_lock, (15, 3))

        # Back
        back_top_left = t_right.crop((14, 45, 29, 50))
        back_top_left.load()
        back_top_right = t_left.crop((14, 45, 29, 50))
        back_top_right.load()

        back_bottom_left = t_right.crop((14, 21, 29, 31))
        back_bottom_left.load()
        back_bottom_right = t_left.crop((14, 21, 29, 31))
        back_bottom_right.load()

        back = Image.new("RGBA", (32, 16), self.bgcolor)
        alpha_over(back, back_top_left, (1, 1))
        alpha_over(back, back_top_right, (16, 1))
        alpha_over(back, back_bottom_left, (1, 5))
        alpha_over(back, back_bottom_right, (16, 5))
        
        # left side
        side_l_top = t_left.crop((29, 45, 43, 50))
        side_l_top.load()
        side_l_bottom = t_left.crop((29, 21, 43, 31))
        side_l_bottom.load()
        side_l = Image.new("RGBA", (16, 16), self.bgcolor)
        alpha_over(side_l, side_l_top, (1, 1))
        alpha_over(side_l, side_l_bottom, (1, 5))
        # right side
        side_r_top = t_right.crop((0, 45, 14, 50))
        side_r_top.load()
        side_r_bottom = t_right.crop((0, 21, 14, 31))
        side_r_bottom.load()
        side_r = Image.new("RGBA", (16, 16), self.bgcolor)
        alpha_over(side_r, side_r_top, (1, 1))
        alpha_over(side_r, side_r_bottom, (1, 5))

        # double chest, left half
        if ((data & 24 == 8 and data & 7 in [3, 5]) or (data & 24 == 16 and data & 7 in [2, 4])):
            top = top.crop((0, 0, 16, 16))
            top.load()
            front = front.crop((0, 0, 16, 16))
            front.load()
            back = back.crop((0, 0, 16, 16))
            back.load()
            #~ side = side_l

        # double chest, right half
        elif ((data & 24 == 16 and data & 7 in [3, 5]) or (data & 24 == 8 and data & 7 in [2, 4])):
            top = top.crop((16, 0, 32, 16))
            top.load()
            front = front.crop((16, 0, 32, 16))
            front.load()
            back = back.crop((16, 0, 32, 16))
            back.load()
            #~ side = side_r

        else: # just in case
            return None

    # compose the final block
    img = Image.new("RGBA", (24, 24), self.bgcolor)
    if data & 7 == 2: # north
        side = self.transform_image_side(side_r)
        alpha_over(img, side, (1, 7))
        back = self.transform_image_side(back)
        alpha_over(img, back.transpose(Image.FLIP_LEFT_RIGHT), (11, 7))
        front = self.transform_image_side(front)
        top = self.transform_image_top(top.rotate(180))
        alpha_over(img, top, (0, 2))

    elif data & 7 == 3: # south
        side = self.transform_image_side(side_l)
        alpha_over(img, side, (1, 7))
        front = self.transform_image_side(front).transpose(Image.FLIP_LEFT_RIGHT)
        top = self.transform_image_top(top.rotate(180))
        alpha_over(img, top, (0, 2))
        alpha_over(img, front, (11, 7))

    elif data & 7 == 4: # west
        side = self.transform_image_side(side_r)
        alpha_over(img, side.transpose(Image.FLIP_LEFT_RIGHT), (11, 7))
        front = self.transform_image_side(front)
        alpha_over(img, front, (1, 7))
        top = self.transform_image_top(top.rotate(270))
        alpha_over(img, top, (0, 2))

    elif data & 7 == 5: # east
        back = self.transform_image_side(back)
        side = self.transform_image_side(side_l).transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, side, (11, 7))
        alpha_over(img, back, (1, 7))
        top = self.transform_image_top(top.rotate(270))
        alpha_over(img, top, (0, 2))
        
    else: # just in case
        img = None

    return img

# redstone wire
# uses pseudo-ancildata found in iterate.c
@material(blockid=55, data=list(range(128)), transparent=True)
def wire(self, blockid, data):

    if data & 0b1000000 == 64: # powered redstone wire
        redstone_wire_t = self.load_image_texture("assets/minecraft/textures/block/redstone_dust_line0.png").rotate(90)
        redstone_wire_t = self.tint_texture(redstone_wire_t,(255,0,0))

        redstone_cross_t = self.load_image_texture("assets/minecraft/textures/block/redstone_dust_dot.png")
        redstone_cross_t = self.tint_texture(redstone_cross_t,(255,0,0))

        
    else: # unpowered redstone wire
        redstone_wire_t = self.load_image_texture("assets/minecraft/textures/block/redstone_dust_line0.png").rotate(90)
        redstone_wire_t = self.tint_texture(redstone_wire_t,(48,0,0))
        
        redstone_cross_t = self.load_image_texture("assets/minecraft/textures/block/redstone_dust_dot.png")
        redstone_cross_t = self.tint_texture(redstone_cross_t,(48,0,0))

    # generate an image per redstone direction
    branch_top_left = redstone_cross_t.copy()
    ImageDraw.Draw(branch_top_left).rectangle((0,0,4,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(branch_top_left).rectangle((11,0,15,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(branch_top_left).rectangle((0,11,15,15),outline=(0,0,0,0),fill=(0,0,0,0))
    
    branch_top_right = redstone_cross_t.copy()
    ImageDraw.Draw(branch_top_right).rectangle((0,0,15,4),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(branch_top_right).rectangle((0,0,4,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(branch_top_right).rectangle((0,11,15,15),outline=(0,0,0,0),fill=(0,0,0,0))
    
    branch_bottom_right = redstone_cross_t.copy()
    ImageDraw.Draw(branch_bottom_right).rectangle((0,0,15,4),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(branch_bottom_right).rectangle((0,0,4,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(branch_bottom_right).rectangle((11,0,15,15),outline=(0,0,0,0),fill=(0,0,0,0))

    branch_bottom_left = redstone_cross_t.copy()
    ImageDraw.Draw(branch_bottom_left).rectangle((0,0,15,4),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(branch_bottom_left).rectangle((11,0,15,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(branch_bottom_left).rectangle((0,11,15,15),outline=(0,0,0,0),fill=(0,0,0,0))
            
    # generate the bottom texture
    if data & 0b111111 == 0:
        bottom = redstone_cross_t.copy()

    # see iterate.c for where these masks come from
    has_x = (data & 0b1010) > 0
    has_z = (data & 0b0101) > 0
    if has_x and has_z:
        bottom = redstone_cross_t.copy()
        if has_x:
            alpha_over(bottom, redstone_wire_t.copy())
        if has_z:
            alpha_over(bottom, redstone_wire_t.copy().rotate(90))

    else:
        if has_x:
            bottom = redstone_wire_t.copy()
        elif has_z:
            bottom = redstone_wire_t.copy().rotate(90)
        elif data & 0b1111 == 0: 
            bottom = redstone_cross_t.copy()

    # check for going up redstone wire
    if data & 0b100000 == 32:
        side1 = redstone_wire_t.rotate(90)
    else:
        side1 = None
        
    if data & 0b010000 == 16:
        side2 = redstone_wire_t.rotate(90)
    else:
        side2 = None
        
    img = self.build_full_block(None,side1,side2,None,None,bottom)

    return img

# mineral overlay
# diamond ore
solidmodelblock(blockid=56, name="diamond_ore")
# diamond block
solidmodelblock(blockid=57, name="diamond_block")


@material(blockid=11366, data=list(range(8)), transparent=True, solid=True, nospawn=True)
def lectern(self, blockid, data):
    # Do rotation, mask to not clobber book data
    data = data & 0b100 | ((self.rotation + (data & 0b11)) % 4)

    # Load textures
    base_raw_t = self.load_image_texture("assets/minecraft/textures/block/lectern_base.png")
    front_raw_t = self.load_image_texture("assets/minecraft/textures/block/lectern_front.png")
    side_raw_t = self.load_image_texture("assets/minecraft/textures/block/lectern_sides.png")
    top_raw_t = self.load_image_texture("assets/minecraft/textures/block/lectern_top.png")

    def create_tile(img_src, coord_crop, coord_paste, rot):
        # Takes an image, crops a region, optionally rotates the
        #   texture, then finally pastes it onto a 16x16 image
        img_out = Image.new("RGBA", (16, 16), self.bgcolor)
        img_in = img_src.crop(coord_crop)
        if rot != 0:
            img_in = img_in.rotate(rot, expand=True)
        img_out.paste(img_in, coord_paste)
        return img_out

    def darken_image(img_src, darken_value):
        # Takes an image & alters the brightness, leaving alpha intact
        alpha = img_src.split()[3]
        img_out = ImageEnhance.Brightness(img_src).enhance(darken_value)
        img_out.putalpha(alpha)
        return img_out

    # Generate base
    base_top_t = base_raw_t.rotate([0, 270, 180, 90][data & 0b11])
    # Front & side textures are one pixel taller than they should be
    #   pre-transformation as otherwise the topmost row of pixels
    #   post-transformation are rather transparent, which results in
    #   a visible gap between the base's sides & top
    base_front_t = create_tile(base_raw_t, (0, 13, 16, 16), (0, 13), 0)
    base_side_t = create_tile(base_raw_t, (0, 5, 16, 8), (0, 13), 0)
    base_side3_t = base_front_t if data & 0b11 == 1 else base_side_t
    base_side4_t = base_front_t if data & 0b11 == 0 else base_side_t
    img = self.build_full_block((base_top_t, 14), None, None, base_side3_t, base_side4_t, None)

    # Generate central pillar
    side_flip_t = side_raw_t.transpose(Image.FLIP_LEFT_RIGHT)
    # Define parameters used to obtain the texture for each side
    pillar_param = [{'img': front_raw_t, 'crop': (8, 4, 16, 16), 'paste': (4, 2), 'rot': 0},    # South
                    {'img': side_raw_t,  'crop': (2, 8, 15, 16), 'paste': (4, 1), 'rot': 270},  # West
                    {'img': front_raw_t, 'crop': (0, 4,  8, 13), 'paste': (4, 5), 'rot': 0},    # North
                    {'img': side_flip_t, 'crop': (2, 8, 15, 16), 'paste': (4, 1), 'rot': 90}]   # East
    # Determine which sides are rendered
    pillar_side = [pillar_param[(3 - (data & 0b11)) % 4], pillar_param[(2 - (data & 0b11)) % 4]]

    pillar_side3_t = create_tile(pillar_side[0]['img'], pillar_side[0]['crop'],
                                 pillar_side[0]['paste'], pillar_side[0]['rot'])
    pillar_side4_t = create_tile(pillar_side[1]['img'], pillar_side[1]['crop'],
                                 pillar_side[1]['paste'], pillar_side[1]['rot'])
    pillar_side4_t = pillar_side4_t.transpose(Image.FLIP_LEFT_RIGHT)
    pillar_side3_t = self.transform_image_side(pillar_side3_t)
    pillar_side3_t = darken_image(pillar_side3_t, 0.9)
    pillar_side4_t = self.transform_image_side(pillar_side4_t).transpose(Image.FLIP_LEFT_RIGHT)
    pillar_side4_t = darken_image(pillar_side4_t, 0.8)
    alpha_over(img, pillar_side3_t, (3, 4), pillar_side3_t)
    alpha_over(img, pillar_side4_t, (9, 4), pillar_side4_t)

    # Generate stand
    if (data & 0b11) in [0, 1]:  # South, West
        stand_side3_t = create_tile(side_raw_t, (0, 0, 16, 4), (0, 4), 0)
        stand_side4_t = create_tile(side_raw_t, (0, 4, 13, 8), (0, 0), -22.5)
    else:  # North, East
        stand_side3_t = create_tile(side_raw_t, (0, 4, 16, 8), (0, 0), 0)
        stand_side4_t = create_tile(side_raw_t, (0, 4, 13, 8), (0, 0), 22.5)

    stand_side3_t = self.transform_image_angle(stand_side3_t, math.radians(22.5))
    stand_side3_t = darken_image(stand_side3_t, 0.9)
    stand_side4_t = self.transform_image_side(stand_side4_t).transpose(Image.FLIP_LEFT_RIGHT)
    stand_side4_t = darken_image(stand_side4_t, 0.8)
    stand_top_t = create_tile(top_raw_t, (0, 1, 16, 14), (0, 1), 0)
    if data & 0b100:
        # Lectern has a book, modify the stand top texture
        book_raw_t = self.load_image("assets/minecraft/textures/entity/enchanting_table_book.png")
        book_t = Image.new("RGBA", (14, 10), self.bgcolor)
        book_part_t = book_raw_t.crop((0, 0, 7, 10))  # Left cover
        alpha_over(stand_top_t, book_part_t, (1, 3), book_part_t)
        book_part_t = book_raw_t.crop((15, 0, 22, 10))  # Right cover
        alpha_over(stand_top_t, book_part_t, (8, 3))
        book_part_t = book_raw_t.crop((24, 10, 29, 18)).rotate(180)  # Left page
        alpha_over(stand_top_t, book_part_t, (3, 4), book_part_t)
        book_part_t = book_raw_t.crop((29, 10, 34, 18)).rotate(180)  # Right page
        alpha_over(stand_top_t, book_part_t, (8, 4), book_part_t)

    # Perform affine transformation
    transform_matrix = numpy.matrix(numpy.identity(3))
    if (data & 0b11) in [0, 1]:  # South, West
        # Translate: 8 -X, 8 -Y
        transform_matrix *= numpy.matrix([[1, 0, 8], [0, 1, 8], [0, 0, 1]])
        # Rotate 40 degrees clockwise
        tc = math.cos(math.radians(40))
        ts = math.sin(math.radians(40))
        transform_matrix *= numpy.matrix([[tc, ts, 0], [-ts, tc, 0], [0, 0, 1]])
        # Shear in the Y direction
        tt = math.tan(math.radians(10))
        transform_matrix *= numpy.matrix([[1, 0, 0], [tt, 1, 0], [0, 0, 1]])
        # Scale to 70% height & 110% width
        transform_matrix *= numpy.matrix([[1 / 1.1, 0, 0], [0, 1 / 0.7, 0], [0, 0, 1]])
        # Translate: 12 +X, 8 +Y
        transform_matrix *= numpy.matrix([[1, 0, -12], [0, 1, -8], [0, 0, 1]])
    else:  # North, East
        # Translate: 8 -X, 8 -Y
        transform_matrix *= numpy.matrix([[1, 0, 8], [0, 1, 8], [0, 0, 1]])
        # Shear in the X direction
        tt = math.tan(math.radians(25))
        transform_matrix *= numpy.matrix([[1, tt, 0], [0, 1, 0], [0, 0, 1]])
        # Scale to 80% height
        transform_matrix *= numpy.matrix([[1, 0, 0], [0, 1 / 0.8, 0], [0, 0, 1]])
        # Rotate 220 degrees clockwise
        tc = math.cos(math.radians(40 + 180))
        ts = math.sin(math.radians(40 + 180))
        transform_matrix *= numpy.matrix([[tc, ts, 0], [-ts, tc, 0], [0, 0, 1]])
        # Scale to 60% height
        transform_matrix *= numpy.matrix([[1, 0, 0], [0, 1 / 0.6, 0], [0, 0, 1]])
        # Translate: +13 X, +7 Y
        transform_matrix *= numpy.matrix([[1, 0, -13], [0, 1, -7], [0, 0, 1]])

    transform_matrix = numpy.array(transform_matrix)[:2, :].ravel().tolist()
    stand_top_t = stand_top_t.transform((24, 24), Image.AFFINE, transform_matrix)

    img_stand = Image.new("RGBA", (24, 24), self.bgcolor)
    alpha_over(img_stand, stand_side3_t, (-4, 2), stand_side3_t)  # Fix some holes
    alpha_over(img_stand, stand_side3_t, (-3, 3), stand_side3_t)
    alpha_over(img_stand, stand_side4_t, (12, 5), stand_side4_t)
    alpha_over(img_stand, stand_top_t, (0, 0), stand_top_t)
    # Flip the stand if North or South facing
    if (data & 0b11) in [0, 2]:
        img_stand = img_stand.transpose(Image.FLIP_LEFT_RIGHT)
    alpha_over(img, img_stand, (0, -2), img_stand)

    return img


@material(blockid=11367, data=list(range(4)), solid=True)
def loom(self, blockid, data):
    # normalize data so it can be used by a generic method
    blockstate = {}
    blockstate['facing'] = {0:'south', 1:'west', 2:'north', 3:'east'}[data]
    return self.build_block_from_model('loom', blockstate)

@material(blockid=11368, data=list(range(4)), transparent=True, solid=True, nospawn=True)
def stonecutter(self, blockid, data):
    # Do rotation
    data = (self.rotation + data) % 4

    top_t = self.load_image_texture("assets/minecraft/textures/block/stonecutter_top.png").copy()
    side_t = self.load_image_texture("assets/minecraft/textures/block/stonecutter_side.png")
    # Stonecutter saw texture contains multiple tiles, since it's
    #   16px wide rely on load_image_texture() to crop appropriately
    blade_t = self.load_image_texture("assets/minecraft/textures/block/stonecutter_saw.png").copy()

    top_t = top_t.rotate([180, 90, 0, 270][data])
    img = self.build_full_block((top_t, 7), None, None, side_t, side_t, None)

    # Add saw blade
    if data in [0, 2]:
        blade_t = blade_t.transpose(Image.FLIP_LEFT_RIGHT)
    blade_t = self.transform_image_side(blade_t)
    if data in [0, 2]:
        blade_t = blade_t.transpose(Image.FLIP_LEFT_RIGHT)
    alpha_over(img, blade_t, (6, -4), blade_t)

    return img


@material(blockid=11369, data=list(range(12)), transparent=True, solid=True, nospawn=True)
def grindstone(self, blockid, data):
    # Do rotation, mask to not clobber mounting info
    data = data & 0b1100 | ((self.rotation + (data & 0b11)) % 4)

    # Load textures
    side_raw_t = self.load_image_texture("assets/minecraft/textures/block/grindstone_side.png").copy()
    round_raw_t = self.load_image_texture("assets/minecraft/textures/block/grindstone_round.png").copy()
    pivot_raw_t = self.load_image_texture("assets/minecraft/textures/block/grindstone_pivot.png").copy()
    leg_raw_t = self.load_image_texture("assets/minecraft/textures/block/dark_oak_log.png").copy()

    def create_tile(img_src, coord_crop, coord_paste,  scale):
        # Takes an image, crops a region, optionally scales the
        #   texture, then finally pastes it onto a 16x16 image
        img_out = Image.new("RGBA", (16, 16), self.bgcolor)
        img_in = img_src.crop(coord_crop)
        if scale >= 0 and scale != 1:
            w, h = img_in.size
            img_in = img_in.resize((int(w * scale), int(h * scale)), Image.NEAREST)
        img_out.paste(img_in, coord_paste)
        return img_out

    # Set variables defining positions of various parts
    wall_mounted = bool(data & 0b0100)
    rot_leg = [0, 270, 0][data >> 2]
    if wall_mounted:
        pos_leg = (32, 28) if data & 0b11 in [2, 3] else (10, 18)
        coord_leg = [(0, 0), (-10, -1), (2, 3)]
        offset_final = [(2, 1), (-2, 1), (-2, -1), (2, -1)][data & 0b11]
    else:
        pos_leg = [(22, 31), (22, 9)][data >> 3]
        coord_leg = [(0, 0), (-1, 2), (-2, -3)]
        offset_final = (0, 2 * (data >> 2) - 1)

    # Create parts
    # Scale up small parts like pivot & leg to avoid ugly results
    #   when shearing & combining parts, then scale down to original
    #   size just before final image composition
    scale_factor = 2
    side_t = create_tile(side_raw_t, (0, 0, 12, 12), (2, 0), 1)
    round_ud_t = create_tile(round_raw_t, (0, 0, 8, 12), (4, 2), 1)
    round_lr_t = create_tile(round_raw_t, (0, 0, 8, 12), (4, 0), 1)
    pivot_outer_t = create_tile(pivot_raw_t, (0, 0, 6, 6), (2, 2), scale_factor)
    pivot_lr_t = create_tile(pivot_raw_t, (6, 0, 8, 6), (2, 2), scale_factor)
    pivot_ud_t = create_tile(pivot_raw_t, (8, 0, 10, 6), (2, 2), scale_factor)
    leg_outer_t = create_tile(leg_raw_t, (6, 9, 10, 16), (2, 2), scale_factor).rotate(rot_leg)
    leg_lr_t = create_tile(leg_raw_t, (12, 9, 14, 16), (2, 2), scale_factor).rotate(rot_leg)
    leg_ud_t = create_tile(leg_raw_t, (2, 6, 4, 10), (2, 2), scale_factor)

    # Transform to block sides & tops
    side_t = self.transform_image_side(side_t)
    round_ud_t = self.transform_image_top(round_ud_t)
    round_lr_t = self.transform_image_side(round_lr_t).transpose(Image.FLIP_LEFT_RIGHT)
    pivot_outer_t = self.transform_image_side(pivot_outer_t)
    pivot_lr_t = self.transform_image_side(pivot_lr_t).transpose(Image.FLIP_LEFT_RIGHT)
    pivot_ud_t = self.transform_image_top(pivot_ud_t)
    leg_outer_t = self.transform_image_side(leg_outer_t)
    if wall_mounted:
        leg_lr_t = self.transform_image_top(leg_lr_t).transpose(Image.FLIP_LEFT_RIGHT)
        leg_ud_t = self.transform_image_side(leg_ud_t).transpose(Image.FLIP_LEFT_RIGHT)
    else:
        leg_lr_t = self.transform_image_side(leg_lr_t).transpose(Image.FLIP_LEFT_RIGHT)
        leg_ud_t = self.transform_image_top(leg_ud_t)

    # Compose leg texture
    img_leg = Image.new("RGBA", (24 * scale_factor, 24 * scale_factor), self.bgcolor)
    alpha_over(img_leg, leg_outer_t, coord_leg[0], leg_outer_t)
    alpha_over(img_leg, leg_lr_t, coord_leg[1], leg_lr_t)
    alpha_over(img_leg, leg_ud_t, coord_leg[2], leg_ud_t)

    # Compose pivot texture (& combine with leg)
    img_pivot = Image.new("RGBA", (24 * scale_factor, 24 * scale_factor), self.bgcolor)
    alpha_over(img_pivot, pivot_ud_t, (20, 18), pivot_ud_t)
    alpha_over(img_pivot, pivot_lr_t, (23, 24), pivot_lr_t)  # Fix gaps between face edges
    alpha_over(img_pivot, pivot_lr_t, (24, 24), pivot_lr_t)
    alpha_over(img_pivot, img_leg, pos_leg, img_leg)
    alpha_over(img_pivot, pivot_outer_t, (21, 21), pivot_outer_t)
    if hasattr(Image, "LANCZOS"):   # workaround for older Pillow
        img_pivot = img_pivot.resize((24, 24), Image.LANCZOS)
    else:
        img_pivot = img_pivot.resize((24, 24))

    # Combine leg, side, round & pivot
    img = Image.new("RGBA", (24, 24), self.bgcolor)
    img_final = img.copy()
    alpha_over(img, img_pivot, (1, -5), img_pivot)
    alpha_over(img, round_ud_t, (0, 2), round_ud_t)  # Fix gaps between face edges
    alpha_over(img, side_t, (3, 6), side_t)
    alpha_over(img, round_ud_t, (0, 1), round_ud_t)
    alpha_over(img, round_lr_t, (10, 6), round_lr_t)
    alpha_over(img, img_pivot, (-5, -1), img_pivot)
    if (data & 0b11) in [1, 3]:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    alpha_over(img_final, img, offset_final, img)

    return img_final


# crops with 8 data values (like wheat)
@material(blockid=59, data=list(range(8)), transparent=True, nospawn=True)
def crops8(self, blockid, data):
    return self.build_block_from_model("wheat_stage%d" % data)

# farmland and grass path (15/16 blocks)
@material(blockid=[60, 208], data=list(range(2)), solid=True, transparent=True, nospawn=True)
def farmland(self, blockid, data):
    if blockid == 60:
        side = self.load_image_texture("assets/minecraft/textures/block/dirt.png").copy()
        if data == 0:
            top = self.load_image_texture("assets/minecraft/textures/block/farmland.png")
        else:
            top = self.load_image_texture("assets/minecraft/textures/block/farmland_moist.png")
        # dirt.png is 16 pixels tall, so we need to crop it before building full block
        side = side.crop((0, 1, 16, 16))
    else:
        top = self.load_image_texture("assets/minecraft/textures/block/dirt_path_top.png")
        side = self.load_image_texture("assets/minecraft/textures/block/dirt_path_side.png")
        # side already has 1 transparent pixel at the top, so it doesn't need to be modified
        # just shift the top image down 1 pixel

    return self.build_full_block((top, 1), side, side, side, side)


# signposts
@material(blockid=[63,11401,11402,11403,11404,11405,11406,12505,12506], data=list(range(16)), transparent=True)
def signpost(self, blockid, data):

    # first rotations
    if self.rotation == 1:
        data = (data + 4) % 16
    elif self.rotation == 2:
        data = (data + 8) % 16
    elif self.rotation == 3:
        data = (data + 12) % 16
    
    sign_texture = {
        # (texture on sign, texture on stick)
        63: ("oak_planks.png", "oak_log.png"),
        11401: ("oak_planks.png", "oak_log.png"),
        11402: ("spruce_planks.png", "spruce_log.png"),
        11403: ("birch_planks.png", "birch_log.png"),
        11404: ("jungle_planks.png", "jungle_log.png"),
        11405: ("acacia_planks.png", "acacia_log.png"),
        11406: ("dark_oak_planks.png", "dark_oak_log.png"),
        12505: ("crimson_planks.png", "crimson_stem.png"),
        12506: ("warped_planks.png", "warped_stem.png"),
    }
    texture_path, texture_stick_path = ["assets/minecraft/textures/block/" + x for x in sign_texture[blockid]]
    
    texture = self.load_image_texture(texture_path).copy()
    
    # cut the planks to the size of a signpost
    ImageDraw.Draw(texture).rectangle((0,12,15,15),outline=(0,0,0,0),fill=(0,0,0,0))

    # If the signpost is looking directly to the image, draw some 
    # random dots, they will look as text.
    if data in (0,1,2,3,4,5,15):
        for i in range(15):
            x = randint(4,11)
            y = randint(3,7)
            texture.putpixel((x,y),(0,0,0,255))

    # Minecraft uses wood texture for the signpost stick
    texture_stick = self.load_image_texture(texture_stick_path)
    texture_stick = texture_stick.resize((12,12), Image.ANTIALIAS)
    ImageDraw.Draw(texture_stick).rectangle((2,0,12,12),outline=(0,0,0,0),fill=(0,0,0,0))

    img = Image.new("RGBA", (24,24), self.bgcolor)

    #         W                N      ~90       E                   S        ~270
    angles = (330.,345.,0.,15.,30.,55.,95.,120.,150.,165.,180.,195.,210.,230.,265.,310.)
    angle = math.radians(angles[data])
    post = self.transform_image_angle(texture, angle)

    # choose the position of the "3D effect"
    incrementx = 0
    if data in (1,6,7,8,9,14):
        incrementx = -1
    elif data in (3,4,5,11,12,13):
        incrementx = +1

    alpha_over(img, texture_stick,(11, 8),texture_stick)
    # post2 is a brighter signpost pasted with a small shift,
    # gives to the signpost some 3D effect.
    post2 = ImageEnhance.Brightness(post).enhance(1.2)
    alpha_over(img, post2,(incrementx, -3),post2)
    alpha_over(img, post, (0,-2), post)

    return img


# wooden and iron door
# uses pseudo-ancildata found in iterate.c
@material(blockid=[64,71,193,194,195,196,197,457, 499, 500], data=list(range(32)), transparent=True)
def door(self, blockid, data):
    #Masked to not clobber block top/bottom & swung info
    if self.rotation == 1:
        if (data & 0b00011) == 0: data = data & 0b11100 | 1
        elif (data & 0b00011) == 1: data = data & 0b11100 | 2
        elif (data & 0b00011) == 2: data = data & 0b11100 | 3
        elif (data & 0b00011) == 3: data = data & 0b11100 | 0
    elif self.rotation == 2:
        if (data & 0b00011) == 0: data = data & 0b11100 | 2
        elif (data & 0b00011) == 1: data = data & 0b11100 | 3
        elif (data & 0b00011) == 2: data = data & 0b11100 | 0
        elif (data & 0b00011) == 3: data = data & 0b11100 | 1
    elif self.rotation == 3:
        if (data & 0b00011) == 0: data = data & 0b11100 | 3
        elif (data & 0b00011) == 1: data = data & 0b11100 | 0
        elif (data & 0b00011) == 2: data = data & 0b11100 | 1
        elif (data & 0b00011) == 3: data = data & 0b11100 | 2

    if data & 0x8 == 0x8: # top of the door
        if blockid == 64: # classic wood door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/oak_door_top.png")
        elif blockid == 71: # iron door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/iron_door_top.png")
        elif blockid == 193: # spruce door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/spruce_door_top.png")
        elif blockid == 194: # birch door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/birch_door_top.png")
        elif blockid == 195: # jungle door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/jungle_door_top.png")
        elif blockid == 196: # acacia door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/acacia_door_top.png")
        elif blockid == 197: # dark_oak door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/dark_oak_door_top.png")
        elif blockid == 457: # mangrove door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/mangrove_door_top.png")
        elif blockid == 499: # crimson door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/crimson_door_top.png")
        elif blockid == 500: # warped door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/warped_door_top.png")
    else: # bottom of the door
        if blockid == 64:
            raw_door = self.load_image_texture("assets/minecraft/textures/block/oak_door_bottom.png")
        elif blockid == 71: # iron door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/iron_door_bottom.png")
        elif blockid == 193: # spruce door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/spruce_door_bottom.png")
        elif blockid == 194: # birch door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/birch_door_bottom.png")
        elif blockid == 195: # jungle door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/jungle_door_bottom.png")
        elif blockid == 196: # acacia door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/acacia_door_bottom.png")
        elif blockid == 197: # dark_oak door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/dark_oak_door_bottom.png")
        elif blockid == 457: # mangrove door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/mangrove_door_bottom.png")
        elif blockid == 499: # crimson door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/crimson_door_bottom.png")
        elif blockid == 500: # warped door
            raw_door = self.load_image_texture("assets/minecraft/textures/block/warped_door_bottom.png")

    # if you want to render all doors as closed, then force
    # force closed to be True
    if data & 0x4 == 0x4:
        closed = False
    else:
        closed = True
    
    if data & 0x10 == 0x10:
        # hinge on the left (facing same door direction)
        hinge_on_left = True
    else:
        # hinge on the right (default single door)
        hinge_on_left = False

    # mask out the high bits to figure out the orientation 
    img = Image.new("RGBA", (24,24), self.bgcolor)
    if (data & 0x03) == 0: # facing west when closed
        if hinge_on_left:
            if closed:
                tex = self.transform_image_side(raw_door.transpose(Image.FLIP_LEFT_RIGHT))
                alpha_over(img, tex, (0,6), tex)
            else:
                # flip first to set the doornob on the correct side
                tex = self.transform_image_side(raw_door.transpose(Image.FLIP_LEFT_RIGHT))
                tex = tex.transpose(Image.FLIP_LEFT_RIGHT)
                alpha_over(img, tex, (12,6), tex)
        else:
            if closed:
                tex = self.transform_image_side(raw_door)    
                alpha_over(img, tex, (0,6), tex)
            else:
                # flip first to set the doornob on the correct side
                tex = self.transform_image_side(raw_door.transpose(Image.FLIP_LEFT_RIGHT))
                tex = tex.transpose(Image.FLIP_LEFT_RIGHT)
                alpha_over(img, tex, (0,0), tex)
    
    if (data & 0x03) == 1: # facing north when closed
        if hinge_on_left:
            if closed:
                tex = self.transform_image_side(raw_door).transpose(Image.FLIP_LEFT_RIGHT)
                alpha_over(img, tex, (0,0), tex)
            else:
                # flip first to set the doornob on the correct side
                tex = self.transform_image_side(raw_door)
                alpha_over(img, tex, (0,6), tex)

        else:
            if closed:
                tex = self.transform_image_side(raw_door).transpose(Image.FLIP_LEFT_RIGHT)
                alpha_over(img, tex, (0,0), tex)
            else:
                # flip first to set the doornob on the correct side
                tex = self.transform_image_side(raw_door)
                alpha_over(img, tex, (12,0), tex)

                
    if (data & 0x03) == 2: # facing east when closed
        if hinge_on_left:
            if closed:
                tex = self.transform_image_side(raw_door)
                alpha_over(img, tex, (12,0), tex)
            else:
                # flip first to set the doornob on the correct side
                tex = self.transform_image_side(raw_door)
                tex = tex.transpose(Image.FLIP_LEFT_RIGHT)
                alpha_over(img, tex, (0,0), tex)
        else:
            if closed:
                tex = self.transform_image_side(raw_door.transpose(Image.FLIP_LEFT_RIGHT))
                alpha_over(img, tex, (12,0), tex)
            else:
                # flip first to set the doornob on the correct side
                tex = self.transform_image_side(raw_door).transpose(Image.FLIP_LEFT_RIGHT)
                alpha_over(img, tex, (12,6), tex)

    if (data & 0x03) == 3: # facing south when closed
        if hinge_on_left:
            if closed:
                tex = self.transform_image_side(raw_door).transpose(Image.FLIP_LEFT_RIGHT)
                alpha_over(img, tex, (12,6), tex)
            else:
                # flip first to set the doornob on the correct side
                tex = self.transform_image_side(raw_door.transpose(Image.FLIP_LEFT_RIGHT))
                alpha_over(img, tex, (12,0), tex)
        else:
            if closed:
                tex = self.transform_image_side(raw_door.transpose(Image.FLIP_LEFT_RIGHT))
                tex = tex.transpose(Image.FLIP_LEFT_RIGHT)
                alpha_over(img, tex, (12,6), tex)
            else:
                # flip first to set the doornob on the correct side
                tex = self.transform_image_side(raw_door.transpose(Image.FLIP_LEFT_RIGHT))
                alpha_over(img, tex, (0,6), tex)

    return img

# ladder
@material(blockid=65, data=[2, 3, 4, 5], transparent=True)
def ladder(self, blockid, data):

    # first rotations
    if self.rotation == 1:
        if data == 2: data = 5
        elif data == 3: data = 4
        elif data == 4: data = 2
        elif data == 5: data = 3
    elif self.rotation == 2:
        if data == 2: data = 3
        elif data == 3: data = 2
        elif data == 4: data = 5
        elif data == 5: data = 4
    elif self.rotation == 3:
        if data == 2: data = 4
        elif data == 3: data = 5
        elif data == 4: data = 3
        elif data == 5: data = 2

    img = Image.new("RGBA", (24,24), self.bgcolor)
    raw_texture = self.load_image_texture("assets/minecraft/textures/block/ladder.png")

    if data == 5:
        # normally this ladder would be obsured by the block it's attached to
        # but since ladders can apparently be placed on transparent blocks, we 
        # have to render this thing anyway.  same for data == 2
        tex = self.transform_image_side(raw_texture)
        alpha_over(img, tex, (0,6), tex)
        return img
    if data == 2:
        tex = self.transform_image_side(raw_texture).transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, tex, (12,6), tex)
        return img
    if data == 3:
        tex = self.transform_image_side(raw_texture).transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, tex, (0,0), tex)
        return img
    if data == 4:
        tex = self.transform_image_side(raw_texture)
        alpha_over(img, tex, (12,0), tex)
        return img


# wall signs
@material(blockid=[68,454,11407,11408,11409,11410,11411,11412,12507,12508], data=[2, 3, 4, 5], transparent=True)
def wall_sign(self, blockid, data): # wall sign

    # first rotations
    if self.rotation == 1:
        if data == 2: data = 5
        elif data == 3: data = 4
        elif data == 4: data = 2
        elif data == 5: data = 3
    elif self.rotation == 2:
        if data == 2: data = 3
        elif data == 3: data = 2
        elif data == 4: data = 5
        elif data == 5: data = 4
    elif self.rotation == 3:
        if data == 2: data = 4
        elif data == 3: data = 5
        elif data == 4: data = 3
        elif data == 5: data = 2
    
    sign_texture = {
        68: "oak_planks.png",
        454: "mangrove_planks.png",
        11407: "oak_planks.png",
        11408: "spruce_planks.png",
        11409: "birch_planks.png",
        11410: "jungle_planks.png",
        11411: "acacia_planks.png",
        11412: "dark_oak_planks.png",
        12507: "crimson_planks.png",
        12508: "warped_planks.png",
    }
    texture_path = "assets/minecraft/textures/block/" + sign_texture[blockid]
    texture = self.load_image_texture(texture_path).copy()
    # cut the planks to the size of a signpost
    ImageDraw.Draw(texture).rectangle((0,12,15,15),outline=(0,0,0,0),fill=(0,0,0,0))

    # draw some random black dots, they will look as text
    """ don't draw text at the moment, they are used in blank for decoration
    
    if data in (3,4):
        for i in range(15):
            x = randint(4,11)
            y = randint(3,7)
            texture.putpixel((x,y),(0,0,0,255))
    """
    
    img = Image.new("RGBA", (24,24), self.bgcolor)

    incrementx = 0
    if data == 2:  # east
        incrementx = +1
        sign = self.build_full_block(None, None, None, None, texture)
    elif data == 3:  # west
        incrementx = -1
        sign = self.build_full_block(None, texture, None, None, None)
    elif data == 4:  # north
        incrementx = +1
        sign = self.build_full_block(None, None, texture, None, None)
    elif data == 5:  # south
        incrementx = -1
        sign = self.build_full_block(None, None, None, texture, None)

    sign2 = ImageEnhance.Brightness(sign).enhance(1.2)
    alpha_over(img, sign2,(incrementx, 2),sign2)
    alpha_over(img, sign, (0,3), sign)

    return img

# levers
@material(blockid=69, data=list(range(16)), transparent=True)
def levers(self, blockid, data):
    if data & 8 == 8: powered = True
    else: powered = False

    data = data & 7

    # first rotations
    if self.rotation == 1:
        # on wall levers
        if data == 1: data = 3
        elif data == 2: data = 4
        elif data == 3: data = 2
        elif data == 4: data = 1
        # on floor levers
        elif data == 5: data = 6
        elif data == 6: data = 5
    elif self.rotation == 2:
        if data == 1: data = 2
        elif data == 2: data = 1
        elif data == 3: data = 4
        elif data == 4: data = 3
        elif data == 5: data = 5
        elif data == 6: data = 6
    elif self.rotation == 3:
        if data == 1: data = 4
        elif data == 2: data = 3
        elif data == 3: data = 1
        elif data == 4: data = 2
        elif data == 5: data = 6
        elif data == 6: data = 5

    # generate the texture for the base of the lever
    t_base = self.load_image_texture("assets/minecraft/textures/block/stone.png").copy()

    ImageDraw.Draw(t_base).rectangle((0,0,15,3),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(t_base).rectangle((0,12,15,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(t_base).rectangle((0,0,4,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(t_base).rectangle((11,0,15,15),outline=(0,0,0,0),fill=(0,0,0,0))

    # generate the texture for the stick
    stick = self.load_image_texture("assets/minecraft/textures/block/lever.png").copy()
    c_stick = Image.new("RGBA", (16,16), self.bgcolor)
    
    tmp = ImageEnhance.Brightness(stick).enhance(0.8)
    alpha_over(c_stick, tmp, (1,0), tmp)
    alpha_over(c_stick, stick, (0,0), stick)
    t_stick = self.transform_image_side(c_stick.rotate(45, Image.NEAREST))

    # where the lever will be composed
    img = Image.new("RGBA", (24,24), self.bgcolor)
    
    # wall levers
    if data == 1: # facing SOUTH
        # levers can't be placed in transparent blocks, so this
        # direction is almost invisible
        return None

    elif data == 2: # facing NORTH
        base = self.transform_image_side(t_base)
        
        # paste it twice with different brightness to make a fake 3D effect
        alpha_over(img, base, (12,-1), base)

        alpha = base.split()[3]
        base = ImageEnhance.Brightness(base).enhance(0.9)
        base.putalpha(alpha)
        
        alpha_over(img, base, (11,0), base)

        # paste the lever stick
        pos = (7,-7)
        if powered:
            t_stick = t_stick.transpose(Image.FLIP_TOP_BOTTOM)
            pos = (7,6)
        alpha_over(img, t_stick, pos, t_stick)

    elif data == 3: # facing WEST
        base = self.transform_image_side(t_base)
        
        # paste it twice with different brightness to make a fake 3D effect
        base = base.transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, base, (0,-1), base)

        alpha = base.split()[3]
        base = ImageEnhance.Brightness(base).enhance(0.9)
        base.putalpha(alpha)
        
        alpha_over(img, base, (1,0), base)
        
        # paste the lever stick
        t_stick = t_stick.transpose(Image.FLIP_LEFT_RIGHT)
        pos = (5,-7)
        if powered:
            t_stick = t_stick.transpose(Image.FLIP_TOP_BOTTOM)
            pos = (6,6)
        alpha_over(img, t_stick, pos, t_stick)

    elif data == 4: # facing EAST
        # levers can't be placed in transparent blocks, so this
        # direction is almost invisible
        return None

    # floor levers
    elif data == 5: # pointing south when off
        # lever base, fake 3d again
        base = self.transform_image_top(t_base)

        alpha = base.split()[3]
        tmp = ImageEnhance.Brightness(base).enhance(0.8)
        tmp.putalpha(alpha)
        
        alpha_over(img, tmp, (0,12), tmp)
        alpha_over(img, base, (0,11), base)

        # lever stick
        pos = (3,2)
        if not powered:
            t_stick = t_stick.transpose(Image.FLIP_LEFT_RIGHT)
            pos = (11,2)
        alpha_over(img, t_stick, pos, t_stick)

    elif data == 6: # pointing east when off
        # lever base, fake 3d again
        base = self.transform_image_top(t_base.rotate(90))

        alpha = base.split()[3]
        tmp = ImageEnhance.Brightness(base).enhance(0.8)
        tmp.putalpha(alpha)
        
        alpha_over(img, tmp, (0,12), tmp)
        alpha_over(img, base, (0,11), base)

        # lever stick
        pos = (2,3)
        if not powered:
            t_stick = t_stick.transpose(Image.FLIP_LEFT_RIGHT)
            pos = (10,2)
        alpha_over(img, t_stick, pos, t_stick)

    return img

# wooden and stone pressure plates, and weighted pressure plates
@material(blockid=[70, 72,147,148,1127,11301,11302,11303,11304,11305, 1033,11517,11518], data=[0,1], transparent=True)
def pressure_plate(self, blockid, data):
    texture_name = {70:"assets/minecraft/textures/block/stone.png",              # stone
                    72:"assets/minecraft/textures/block/oak_planks.png",         # oak
                    11301:"assets/minecraft/textures/block/spruce_planks.png",   # spruce
                    11302:"assets/minecraft/textures/block/birch_planks.png",    # birch
                    11303:"assets/minecraft/textures/block/jungle_planks.png",   # jungle
                    11304:"assets/minecraft/textures/block/acacia_planks.png",   # acacia
                    11305:"assets/minecraft/textures/block/dark_oak_planks.png", # dark oak
                    11517:"assets/minecraft/textures/block/crimson_planks.png",  # crimson
                    11518:"assets/minecraft/textures/block/warped_planks.png",   # warped
                    147:"assets/minecraft/textures/block/gold_block.png",        # light golden
                    148:"assets/minecraft/textures/block/iron_block.png",        # heavy iron
                    1033:"assets/minecraft/textures/block/polished_blackstone.png",
                    1127:"assets/minecraft/textures/block/mangrove_planks.png"
                   }[blockid]
    t = self.load_image_texture(texture_name).copy()
    
    # cut out the outside border, pressure plates are smaller
    # than a normal block
    ImageDraw.Draw(t).rectangle((0,0,15,15),outline=(0,0,0,0))
    
    # create the textures and a darker version to make a 3d by 
    # pasting them with an offstet of 1 pixel
    img = Image.new("RGBA", (24,24), self.bgcolor)
    
    top = self.transform_image_top(t)
    
    alpha = top.split()[3]
    topd = ImageEnhance.Brightness(top).enhance(0.8)
    topd.putalpha(alpha)
    
    #show it 3d or 2d if unpressed or pressed
    if data == 0:
        alpha_over(img,topd, (0,12),topd)
        alpha_over(img,top, (0,11),top)
    elif data == 1:
        alpha_over(img,top, (0,12),top)
    
    return img

# mineral overlay
# normal and glowing redstone ore
solidmodelblock(blockid=[73], name="redstone_ore")

# stone and wood buttons
@material(blockid=(77,143,1128,11326,11327,11328,11329,11330,1034,11515,11516), data=list(range(16)), transparent=True)
def buttons(self, blockid, data):

    # 0x8 is set if the button is pressed mask this info and render
    # it as unpressed
    data = data & 0x7

    if self.rotation == 1:
        if data == 1: data = 3
        elif data == 2: data = 4
        elif data == 3: data = 2
        elif data == 4: data = 1
        elif data == 5: data = 6
        elif data == 6: data = 5
    elif self.rotation == 2:
        if data == 1: data = 2
        elif data == 2: data = 1
        elif data == 3: data = 4
        elif data == 4: data = 3
    elif self.rotation == 3:
        if data == 1: data = 4
        elif data == 2: data = 3
        elif data == 3: data = 1
        elif data == 4: data = 2
        elif data == 5: data = 6
        elif data == 6: data = 5

    texturepath = {77:"assets/minecraft/textures/block/stone.png",
                   143:"assets/minecraft/textures/block/oak_planks.png",
                   1128:"assets/minecraft/textures/block/mangrove_planks.png",
                   11326:"assets/minecraft/textures/block/spruce_planks.png",
                   11327:"assets/minecraft/textures/block/birch_planks.png",
                   11328:"assets/minecraft/textures/block/jungle_planks.png",
                   11329:"assets/minecraft/textures/block/acacia_planks.png",
                   11330:"assets/minecraft/textures/block/dark_oak_planks.png",
                   1034:"assets/minecraft/textures/block/polished_blackstone.png",
                   11515:"assets/minecraft/textures/block/crimson_planks.png",
                   11516:"assets/minecraft/textures/block/warped_planks.png"
                  }[blockid]
    t = self.load_image_texture(texturepath).copy()

    # generate the texture for the button
    ImageDraw.Draw(t).rectangle((0,0,15,5),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(t).rectangle((0,10,15,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(t).rectangle((0,0,4,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(t).rectangle((11,0,15,15),outline=(0,0,0,0),fill=(0,0,0,0))

    img = Image.new("RGBA", (24,24), self.bgcolor)

    if data < 5:
        button = self.transform_image_side(t)

        if data == 1: # facing SOUTH
            # buttons can't be placed in transparent blocks, so this
            # direction can't be seen
            return None

        elif data == 2: # facing NORTH
            # paste it twice with different brightness to make a 3D effect
            alpha_over(img, button, (12,-1), button)

            alpha = button.split()[3]
            button = ImageEnhance.Brightness(button).enhance(0.9)
            button.putalpha(alpha)

            alpha_over(img, button, (11,0), button)

        elif data == 3: # facing WEST
            # paste it twice with different brightness to make a 3D effect
            button = button.transpose(Image.FLIP_LEFT_RIGHT)
            alpha_over(img, button, (0,-1), button)

            alpha = button.split()[3]
            button = ImageEnhance.Brightness(button).enhance(0.9)
            button.putalpha(alpha)

            alpha_over(img, button, (1,0), button)

        elif data == 4: # facing EAST
            # buttons can't be placed in transparent blocks, so this
            # direction can't be seen
            return None

    else:
        if data == 5: # long axis east-west
            button = self.transform_image_top(t)
        else: # long axis north-south
            button = self.transform_image_top(t.rotate(90))

        # paste it twice with different brightness to make a 3D effect
        alpha_over(img, button, (0,12), button)

        alpha = button.split()[3]
        button = ImageEnhance.Brightness(button).enhance(0.9)
        button.putalpha(alpha)

        alpha_over(img, button, (0,11), button)

    return img

# end rod
@material(blockid=198, data=list(range(6)), transparent=True, solid=True)
def end_rod(self, blockid, data):
    tex = self.load_image_texture("assets/minecraft/textures/block/end_rod.png")
    img = Image.new("RGBA", (24, 24), self.bgcolor)

    mask = tex.crop((0, 0, 2, 15))
    sidetex = Image.new(tex.mode, tex.size, self.bgcolor)
    alpha_over(sidetex, mask, (14, 0), mask)

    mask = tex.crop((2, 3, 6, 7))
    bottom = Image.new(tex.mode, tex.size, self.bgcolor)
    alpha_over(bottom, mask, (5, 6), mask)

    if data == 1 or data == 0:
        side = self.transform_image_side(sidetex)
        otherside = side.transpose(Image.FLIP_LEFT_RIGHT)
        bottom = self.transform_image_top(bottom)

        if data == 1: # up
            mask = tex.crop((2, 0, 4, 2))
            top = Image.new(tex.mode, tex.size, self.bgcolor)
            alpha_over(top, mask, (7, 2), mask)
            top = self.transform_image_top(top)

            alpha_over(img, bottom, (0, 11), bottom)
            alpha_over(img, side, (0, 0), side)
            alpha_over(img, otherside, (11, 0), otherside)
            alpha_over(img, top, (3, 1), top)
        elif data == 0: # down
            alpha_over(img, side, (0, 0), side)
            alpha_over(img, otherside, (11, 0), otherside)
            alpha_over(img, bottom, (0, 0), bottom)
    else:
        otherside = self.transform_image_top(sidetex)

        sidetex = sidetex.rotate(90)
        side = self.transform_image_side(sidetex)
        bottom = self.transform_image_side(bottom)
        bottom = bottom.transpose(Image.FLIP_LEFT_RIGHT)

        def draw_south():
            alpha_over(img, bottom, (0, 0), bottom)
            alpha_over(img, side, (7, 8), side)
            alpha_over(img, otherside, (-3, 9), otherside)

        def draw_north():
            alpha_over(img, side, (7, 8), side)
            alpha_over(img, otherside, (-3, 9), otherside)
            alpha_over(img, bottom, (12, 6), bottom)

        def draw_west():
            _bottom = bottom.transpose(Image.FLIP_LEFT_RIGHT)
            alpha_over(img, _bottom, (13, 0), _bottom)
            _side = side.transpose(Image.FLIP_LEFT_RIGHT)
            alpha_over(img, _side, (7, 8), _side)
            _otherside = otherside.transpose(Image.FLIP_LEFT_RIGHT)
            alpha_over(img, _otherside, (4, 9), _otherside)

        def draw_east():
            _side = side.transpose(Image.FLIP_LEFT_RIGHT)
            alpha_over(img, _side, (7, 8), _side)
            _otherside = otherside.transpose(Image.FLIP_LEFT_RIGHT)
            alpha_over(img, _otherside, (4, 9), _otherside)
            _bottom = bottom.transpose(Image.FLIP_LEFT_RIGHT)
            alpha_over(img, _bottom, (0, 6), _bottom)

        draw_funcs = [ draw_south, draw_west, draw_north, draw_east ]

        if data == 3: # south
            draw_funcs[self.rotation]()
        elif data == 2: # north
            draw_funcs[(self.rotation + 2) % len(draw_funcs)]()
        elif data == 4: # west
            draw_funcs[(self.rotation + 1) % len(draw_funcs)]()
        elif data == 5: # east
            draw_funcs[(self.rotation + 3) % len(draw_funcs)]()

    return img

# snow
@material(blockid=78, data=list(range(1, 9)), transparent=True, solid=True)
def snow(self, blockid, data):
    tex = self.load_image_texture("assets/minecraft/textures/block/snow.png")

    y = 16 - (data * 2)
    mask = tex.crop((0, y, 16, 16))
    sidetex = Image.new(tex.mode, tex.size, self.bgcolor)
    alpha_over(sidetex, mask, (0,y,16,16), mask)

    img = Image.new("RGBA", (24,24), self.bgcolor)

    top = self.transform_image_top(tex)
    side = self.transform_image_side(sidetex)
    otherside = side.transpose(Image.FLIP_LEFT_RIGHT)

    sidealpha = side.split()[3]
    side = ImageEnhance.Brightness(side).enhance(0.9)
    side.putalpha(sidealpha)
    othersidealpha = otherside.split()[3]
    otherside = ImageEnhance.Brightness(otherside).enhance(0.8)
    otherside.putalpha(othersidealpha)

    alpha_over(img, side, (0, 6), side)
    alpha_over(img, otherside, (12, 6), otherside)
    alpha_over(img, top, (0, 12 - int(12 / 8 * data)), top)

    return img

# cactus
@material(blockid=81, data=list(range(15)), transparent=True, solid=True, nospawn=True)
def cactus(self, blockid, data):
    top = self.load_image_texture("assets/minecraft/textures/block/cactus_top.png")
    side = self.load_image_texture("assets/minecraft/textures/block/cactus_side.png")

    img = Image.new("RGBA", (24,24), self.bgcolor)
    
    top = self.transform_image_top(top)
    side = self.transform_image_side(side)
    otherside = side.transpose(Image.FLIP_LEFT_RIGHT)

    sidealpha = side.split()[3]
    side = ImageEnhance.Brightness(side).enhance(0.9)
    side.putalpha(sidealpha)
    othersidealpha = otherside.split()[3]
    otherside = ImageEnhance.Brightness(otherside).enhance(0.8)
    otherside.putalpha(othersidealpha)

    alpha_over(img, side, (1,6), side)
    alpha_over(img, otherside, (11,6), otherside)
    alpha_over(img, top, (0,0), top)
    
    return img

# sugar cane
@material(blockid=83, data=list(range(16)), transparent=True)
def sugar_cane(self, blockid, data):
    tex = self.load_image_texture("assets/minecraft/textures/block/sugar_cane.png")
    return self.build_sprite(tex)

# nether and normal fences
@material(blockid=[85, 188, 189, 190, 191, 192, 113, 456, 511, 512], data=list(range(16)), transparent=True, nospawn=True)
def fence(self, blockid, data):
    # create needed images for Big stick fence
    if blockid == 85: # normal fence
        fence_top = self.load_image_texture("assets/minecraft/textures/block/oak_planks.png").copy()
        fence_side = self.load_image_texture("assets/minecraft/textures/block/oak_planks.png").copy()
        fence_small_side = self.load_image_texture("assets/minecraft/textures/block/oak_planks.png").copy()
    elif blockid == 188: # spruce fence
        fence_top = self.load_image_texture("assets/minecraft/textures/block/spruce_planks.png").copy()
        fence_side = self.load_image_texture("assets/minecraft/textures/block/spruce_planks.png").copy()
        fence_small_side = self.load_image_texture("assets/minecraft/textures/block/spruce_planks.png").copy()
    elif blockid == 189: # birch fence
        fence_top = self.load_image_texture("assets/minecraft/textures/block/birch_planks.png").copy()
        fence_side = self.load_image_texture("assets/minecraft/textures/block/birch_planks.png").copy()
        fence_small_side = self.load_image_texture("assets/minecraft/textures/block/birch_planks.png").copy()
    elif blockid == 190: # jungle fence
        fence_top = self.load_image_texture("assets/minecraft/textures/block/jungle_planks.png").copy()
        fence_side = self.load_image_texture("assets/minecraft/textures/block/jungle_planks.png").copy()
        fence_small_side = self.load_image_texture("assets/minecraft/textures/block/jungle_planks.png").copy()
    elif blockid == 191: # big/dark oak fence
        fence_top = self.load_image_texture("assets/minecraft/textures/block/dark_oak_planks.png").copy()
        fence_side = self.load_image_texture("assets/minecraft/textures/block/dark_oak_planks.png").copy()
        fence_small_side = self.load_image_texture("assets/minecraft/textures/block/dark_oak_planks.png").copy()
    elif blockid == 192: # acacia fence
        fence_top = self.load_image_texture("assets/minecraft/textures/block/acacia_planks.png").copy()
        fence_side = self.load_image_texture("assets/minecraft/textures/block/acacia_planks.png").copy()
        fence_small_side = self.load_image_texture("assets/minecraft/textures/block/acacia_planks.png").copy()
    elif blockid == 456: # mangrove_fence
        fence_top = self.load_image_texture("assets/minecraft/textures/block/mangrove_planks.png").copy()
        fence_side = self.load_image_texture("assets/minecraft/textures/block/mangrove_planks.png").copy()
        fence_small_side = self.load_image_texture("assets/minecraft/textures/block/mangrove_planks.png").copy()
    elif blockid == 511: # crimson_fence
        fence_top = self.load_image_texture("assets/minecraft/textures/block/crimson_planks.png").copy()
        fence_side = self.load_image_texture("assets/minecraft/textures/block/crimson_planks.png").copy()
        fence_small_side = self.load_image_texture("assets/minecraft/textures/block/crimson_planks.png").copy()
    elif blockid == 512: # warped fence
        fence_top = self.load_image_texture("assets/minecraft/textures/block/warped_planks.png").copy()
        fence_side = self.load_image_texture("assets/minecraft/textures/block/warped_planks.png").copy()
        fence_small_side = self.load_image_texture("assets/minecraft/textures/block/warped_planks.png").copy()
    else: # netherbrick fence
        fence_top = self.load_image_texture("assets/minecraft/textures/block/nether_bricks.png").copy()
        fence_side = self.load_image_texture("assets/minecraft/textures/block/nether_bricks.png").copy()
        fence_small_side = self.load_image_texture("assets/minecraft/textures/block/nether_bricks.png").copy()

    # generate the textures of the fence
    ImageDraw.Draw(fence_top).rectangle((0,0,5,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(fence_top).rectangle((10,0,15,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(fence_top).rectangle((0,0,15,5),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(fence_top).rectangle((0,10,15,15),outline=(0,0,0,0),fill=(0,0,0,0))

    ImageDraw.Draw(fence_side).rectangle((0,0,5,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(fence_side).rectangle((10,0,15,15),outline=(0,0,0,0),fill=(0,0,0,0))

    # Create the sides and the top of the big stick
    fence_side = self.transform_image_side(fence_side)
    fence_other_side = fence_side.transpose(Image.FLIP_LEFT_RIGHT)
    fence_top = self.transform_image_top(fence_top)

    # Darken the sides slightly. These methods also affect the alpha layer,
    # so save them first (we don't want to "darken" the alpha layer making
    # the block transparent)
    sidealpha = fence_side.split()[3]
    fence_side = ImageEnhance.Brightness(fence_side).enhance(0.9)
    fence_side.putalpha(sidealpha)
    othersidealpha = fence_other_side.split()[3]
    fence_other_side = ImageEnhance.Brightness(fence_other_side).enhance(0.8)
    fence_other_side.putalpha(othersidealpha)

    # Compose the fence big stick
    fence_big = Image.new("RGBA", (24,24), self.bgcolor)
    alpha_over(fence_big,fence_side, (5,4),fence_side)
    alpha_over(fence_big,fence_other_side, (7,4),fence_other_side)
    alpha_over(fence_big,fence_top, (0,0),fence_top)
    
    # Now render the small sticks.
    # Create needed images
    ImageDraw.Draw(fence_small_side).rectangle((0,0,15,0),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(fence_small_side).rectangle((0,4,15,6),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(fence_small_side).rectangle((0,10,15,16),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(fence_small_side).rectangle((0,0,4,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(fence_small_side).rectangle((11,0,15,15),outline=(0,0,0,0),fill=(0,0,0,0))

    # Create the sides and the top of the small sticks
    fence_small_side = self.transform_image_side(fence_small_side)
    fence_small_other_side = fence_small_side.transpose(Image.FLIP_LEFT_RIGHT)
    
    # Darken the sides slightly. These methods also affect the alpha layer,
    # so save them first (we don't want to "darken" the alpha layer making
    # the block transparent)
    sidealpha = fence_small_other_side.split()[3]
    fence_small_other_side = ImageEnhance.Brightness(fence_small_other_side).enhance(0.9)
    fence_small_other_side.putalpha(sidealpha)
    sidealpha = fence_small_side.split()[3]
    fence_small_side = ImageEnhance.Brightness(fence_small_side).enhance(0.9)
    fence_small_side.putalpha(sidealpha)

    # Create img to compose the fence
    img = Image.new("RGBA", (24,24), self.bgcolor)

    # Position of fence small sticks in img.
    # These postitions are strange because the small sticks of the 
    # fence are at the very left and at the very right of the 16x16 images
    pos_top_left = (2,3)
    pos_top_right = (10,3)
    pos_bottom_right = (10,7)
    pos_bottom_left = (2,7)

    # +x axis points top right direction
    # +y axis points bottom right direction
    # First compose small sticks in the back of the image,
    # then big stick and then small sticks in the front.
    def draw_north():
        alpha_over(img, fence_small_side, pos_top_left, fence_small_side)
    def draw_east():
        alpha_over(img, fence_small_other_side, pos_top_right, fence_small_other_side)
    def draw_south():
        alpha_over(img, fence_small_side, pos_bottom_right, fence_small_side)
    def draw_west():
        alpha_over(img, fence_small_other_side, pos_bottom_left, fence_small_other_side)

    draw_funcs = [draw_north, draw_east, draw_south, draw_west]

    if (data & 0b0001):
        draw_funcs[(self.rotation + 0) % len(draw_funcs)]()
    if (data & 0b0010):
        draw_funcs[(self.rotation + 1) % len(draw_funcs)]()

    alpha_over(img, fence_big, (0, 0), fence_big)

    if (data & 0b0100):
        draw_funcs[(self.rotation + 2) % len(draw_funcs)]()
    if (data & 0b1000):
        draw_funcs[(self.rotation + 3) % len(draw_funcs)]()

    return img

@material(blockid=91, data=list(range(4)), solid=True)
def jack_o_lantern(self, blockid, data):
    # normalize data so it can be used by a generic method
    blockstate = {}
    blockstate['facing'] = {0:'south', 1:'west', 2:'north', 3:'east'}[data]
    return self.build_block_from_model('jack_o_lantern', blockstate)

@material(blockid=11300, data=list(range(4)), solid=True)
def carved_pumpkin(self, blockid, data):
    # normalize data so it can be used by a generic method
    blockstate = {}
    blockstate['facing'] = {0:'south', 1:'west', 2:'north', 3:'east'}[data]
    return self.build_block_from_model('carved_pumpkin', blockstate)

# nether roof
# netherrack
solidmodelblock(blockid=87, name="netherrack")
# soul sand
solidmodelblock(blockid=88, name="soul_sand")

# portal
@material(blockid=90, data=[1, 2, 4, 5, 8, 10], transparent=True)
def portal(self, blockid, data):
    # no rotations, uses pseudo data
    portaltexture = self.load_portal()
    img = Image.new("RGBA", (24,24), self.bgcolor)

    side = self.transform_image_side(portaltexture)
    otherside = side.transpose(Image.FLIP_TOP_BOTTOM)

    if data in (1,4,5):
        alpha_over(img, side, (5,4), side)

    if data in (2,8,10):
        alpha_over(img, otherside, (5,4), otherside)

    return img


# cake!
@material(blockid=92, data=list(range(7)), transparent=True, nospawn=True)
def cake(self, blockid, data):
    # cake textures
    top = self.load_image_texture("assets/minecraft/textures/block/cake_top.png").copy()
    side = self.load_image_texture("assets/minecraft/textures/block/cake_side.png").copy()
    fullside = side.copy()
    inside = self.load_image_texture("assets/minecraft/textures/block/cake_inner.png")

    img = Image.new("RGBA", (24, 24), self.bgcolor)
    if data == 0:  # unbitten cake
        top = self.transform_image_top(top)
        side = self.transform_image_side(side)
        otherside = side.transpose(Image.FLIP_LEFT_RIGHT)

        # darken sides slightly
        sidealpha = side.split()[3]
        side = ImageEnhance.Brightness(side).enhance(0.9)
        side.putalpha(sidealpha)
        othersidealpha = otherside.split()[3]
        otherside = ImageEnhance.Brightness(otherside).enhance(0.8)
        otherside.putalpha(othersidealpha)

        # composite the cake
        alpha_over(img, side, (1, 6), side)
        alpha_over(img, otherside, (11, 5), otherside)  # workaround, fixes a hole
        alpha_over(img, otherside, (12, 6), otherside)
        alpha_over(img, top, (0, 6), top)

    else:
        # cut the textures for a bitten cake
        bite_width = int(14 / 7)  # Cake is 14px wide with 7 slices
        coord = 1 + bite_width * data
        ImageDraw.Draw(side).rectangle((16 - coord, 0, 16, 16), outline=(0, 0, 0, 0),
                                       fill=(0, 0, 0, 0))
        ImageDraw.Draw(top).rectangle((0, 0, coord - 1, 16), outline=(0, 0, 0, 0),
                                      fill=(0, 0, 0, 0))

        # the bitten part of the cake always points to the west
        # composite the cake for every north orientation
        if self.rotation == 0:  # north top-left
            # create right side
            rs = self.transform_image_side(side).transpose(Image.FLIP_LEFT_RIGHT)
            # create bitten side and its coords
            deltax = bite_width * data
            deltay = -1 * data
            if data in [3, 4, 5, 6]:
                deltax -= 1
            ls = self.transform_image_side(inside)
            # create top side
            t = self.transform_image_top(top)
            # darken sides slightly
            sidealpha = ls.split()[3]
            ls = ImageEnhance.Brightness(ls).enhance(0.9)
            ls.putalpha(sidealpha)
            othersidealpha = rs.split()[3]
            rs = ImageEnhance.Brightness(rs).enhance(0.8)
            rs.putalpha(othersidealpha)
            # compose the cake
            alpha_over(img, rs, (12, 6), rs)
            alpha_over(img, ls, (1 + deltax, 6 + deltay), ls)
            alpha_over(img, t, (1, 6), t)

        elif self.rotation == 1:  # north top-right
            # bitten side not shown
            # create left side
            ls = self.transform_image_side(side.transpose(Image.FLIP_LEFT_RIGHT))
            # create top
            t = self.transform_image_top(top.rotate(-90))
            # create right side
            rs = self.transform_image_side(fullside).transpose(Image.FLIP_LEFT_RIGHT)
            # darken sides slightly
            sidealpha = ls.split()[3]
            ls = ImageEnhance.Brightness(ls).enhance(0.9)
            ls.putalpha(sidealpha)
            othersidealpha = rs.split()[3]
            rs = ImageEnhance.Brightness(rs).enhance(0.8)
            rs.putalpha(othersidealpha)
            # compose the cake
            alpha_over(img, ls, (2, 6), ls)
            alpha_over(img, t, (1, 6), t)
            alpha_over(img, rs, (12, 6), rs)

        elif self.rotation == 2:  # north bottom-right
            # bitten side not shown
            # left side
            ls = self.transform_image_side(fullside)
            # top
            t = self.transform_image_top(top.rotate(180))
            # right side
            rs = self.transform_image_side(side.transpose(Image.FLIP_LEFT_RIGHT))
            rs = rs.transpose(Image.FLIP_LEFT_RIGHT)
            # darken sides slightly
            sidealpha = ls.split()[3]
            ls = ImageEnhance.Brightness(ls).enhance(0.9)
            ls.putalpha(sidealpha)
            othersidealpha = rs.split()[3]
            rs = ImageEnhance.Brightness(rs).enhance(0.8)
            rs.putalpha(othersidealpha)
            # compose the cake
            alpha_over(img, ls, (2, 6), ls)
            alpha_over(img, t, (1, 6), t)
            alpha_over(img, rs, (12, 6), rs)

        elif self.rotation == 3:  # north bottom-left
            # create left side
            ls = self.transform_image_side(side)
            # create top
            t = self.transform_image_top(top.rotate(90))
            # create right side and its coords
            deltax = 12 - bite_width * data
            deltay = -1 * data
            if data in [3, 4, 5, 6]:
                deltax += 1
            rs = self.transform_image_side(inside).transpose(Image.FLIP_LEFT_RIGHT)
            # darken sides slightly
            sidealpha = ls.split()[3]
            ls = ImageEnhance.Brightness(ls).enhance(0.9)
            ls.putalpha(sidealpha)
            othersidealpha = rs.split()[3]
            rs = ImageEnhance.Brightness(rs).enhance(0.8)
            rs.putalpha(othersidealpha)
            # compose the cake
            alpha_over(img, ls, (2, 6), ls)
            alpha_over(img, t, (1, 6), t)
            alpha_over(img, rs, (1 + deltax, 6 + deltay), rs)

    return img


# redstone repeaters ON and OFF
@material(blockid=[93,94], data=list(range(16)), transparent=True, nospawn=True)
def repeater(self, blockid, data):
    # rotation
    # Masked to not clobber delay info
    if self.rotation == 1:
        if (data & 0b0011) == 0: data = data & 0b1100 | 1
        elif (data & 0b0011) == 1: data = data & 0b1100 | 2
        elif (data & 0b0011) == 2: data = data & 0b1100 | 3
        elif (data & 0b0011) == 3: data = data & 0b1100 | 0
    elif self.rotation == 2:
        if (data & 0b0011) == 0: data = data & 0b1100 | 2
        elif (data & 0b0011) == 1: data = data & 0b1100 | 3
        elif (data & 0b0011) == 2: data = data & 0b1100 | 0
        elif (data & 0b0011) == 3: data = data & 0b1100 | 1
    elif self.rotation == 3:
        if (data & 0b0011) == 0: data = data & 0b1100 | 3
        elif (data & 0b0011) == 1: data = data & 0b1100 | 0
        elif (data & 0b0011) == 2: data = data & 0b1100 | 1
        elif (data & 0b0011) == 3: data = data & 0b1100 | 2
    
    # generate the diode
    top = self.load_image_texture("assets/minecraft/textures/block/repeater.png") if blockid == 93 else self.load_image_texture("assets/minecraft/textures/block/repeater_on.png")
    side = self.load_image_texture("assets/minecraft/textures/block/smooth_stone_slab_side.png")
    increment = 13
    
    if (data & 0x3) == 0: # pointing east
        pass
    
    if (data & 0x3) == 1: # pointing south
        top = top.rotate(270)

    if (data & 0x3) == 2: # pointing west
        top = top.rotate(180)

    if (data & 0x3) == 3: # pointing north
        top = top.rotate(90)

    img = self.build_full_block( (top, increment), None, None, side, side)

    # compose a "3d" redstone torch
    t = self.load_image_texture("assets/minecraft/textures/block/redstone_torch_off.png").copy() if blockid == 93 else self.load_image_texture("assets/minecraft/textures/block/redstone_torch.png").copy()
    torch = Image.new("RGBA", (24,24), self.bgcolor)
    
    t_crop = t.crop((2,2,14,14))
    slice = t_crop.copy()
    ImageDraw.Draw(slice).rectangle((6,0,12,12),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(slice).rectangle((0,0,4,12),outline=(0,0,0,0),fill=(0,0,0,0))
    
    alpha_over(torch, slice, (6,4))
    alpha_over(torch, t_crop, (5,5))
    alpha_over(torch, t_crop, (6,5))
    alpha_over(torch, slice, (6,6))
    
    # paste redstone torches everywhere!
    # the torch is too tall for the repeater, crop the bottom.
    ImageDraw.Draw(torch).rectangle((0,16,24,24),outline=(0,0,0,0),fill=(0,0,0,0))
    
    # touch up the 3d effect with big rectangles, just in case, for other texture packs
    ImageDraw.Draw(torch).rectangle((0,24,10,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(torch).rectangle((12,15,24,24),outline=(0,0,0,0),fill=(0,0,0,0))
    
    # torch positions for every redstone torch orientation.
    #
    # This is a horrible list of torch orientations. I tried to 
    # obtain these orientations by rotating the positions for one
    # orientation, but pixel rounding is horrible and messes the
    # torches.

    if (data & 0x3) == 0: # pointing east
        if (data & 0xC) == 0: # one tick delay
            moving_torch = (1,1)
            static_torch = (-3,-1)
            
        elif (data & 0xC) == 4: # two ticks delay
            moving_torch = (2,2)
            static_torch = (-3,-1)
            
        elif (data & 0xC) == 8: # three ticks delay
            moving_torch = (3,2)
            static_torch = (-3,-1)
            
        elif (data & 0xC) == 12: # four ticks delay
            moving_torch = (4,3)
            static_torch = (-3,-1)
    
    elif (data & 0x3) == 1: # pointing south
        if (data & 0xC) == 0: # one tick delay
            moving_torch = (1,1)
            static_torch = (5,-1)
            
        elif (data & 0xC) == 4: # two ticks delay
            moving_torch = (0,2)
            static_torch = (5,-1)
            
        elif (data & 0xC) == 8: # three ticks delay
            moving_torch = (-1,2)
            static_torch = (5,-1)
            
        elif (data & 0xC) == 12: # four ticks delay
            moving_torch = (-2,3)
            static_torch = (5,-1)

    elif (data & 0x3) == 2: # pointing west
        if (data & 0xC) == 0: # one tick delay
            moving_torch = (1,1)
            static_torch = (5,3)
            
        elif (data & 0xC) == 4: # two ticks delay
            moving_torch = (0,0)
            static_torch = (5,3)
            
        elif (data & 0xC) == 8: # three ticks delay
            moving_torch = (-1,0)
            static_torch = (5,3)
            
        elif (data & 0xC) == 12: # four ticks delay
            moving_torch = (-2,-1)
            static_torch = (5,3)

    elif (data & 0x3) == 3: # pointing north
        if (data & 0xC) == 0: # one tick delay
            moving_torch = (1,1)
            static_torch = (-3,3)
            
        elif (data & 0xC) == 4: # two ticks delay
            moving_torch = (2,0)
            static_torch = (-3,3)
            
        elif (data & 0xC) == 8: # three ticks delay
            moving_torch = (3,0)
            static_torch = (-3,3)
            
        elif (data & 0xC) == 12: # four ticks delay
            moving_torch = (4,-1)
            static_torch = (-3,3)
    
    # this paste order it's ok for east and south orientation
    # but it's wrong for north and west orientations. But using the
    # default texture pack the torches are small enough to no overlap.
    alpha_over(img, torch, static_torch, torch) 
    alpha_over(img, torch, moving_torch, torch)

    return img

# redstone comparator (149 is inactive, 150 is active)
@material(blockid=[149,150], data=list(range(16)), transparent=True, nospawn=True)
def comparator(self, blockid, data):

    # rotation
    # add self.rotation to the lower 2 bits,  mod 4
    data = data & 0b1100 | (((data & 0b11) + self.rotation) % 4)


    top = self.load_image_texture("assets/minecraft/textures/block/comparator.png") if blockid == 149 else self.load_image_texture("assets/minecraft/textures/block/comparator_on.png")
    side = self.load_image_texture("assets/minecraft/textures/block/smooth_stone_slab_side.png")
    increment = 13

    if (data & 0x3) == 0: # pointing north
        pass
        static_torch = (-3,-1)
        torch = ((0,2),(6,-1))
    
    if (data & 0x3) == 1: # pointing east
        top = top.rotate(270)
        static_torch = (5,-1)
        torch = ((-4,-1),(0,2))

    if (data & 0x3) == 2: # pointing south
        top = top.rotate(180)
        static_torch = (5,3)
        torch = ((0,-4),(-4,-1))

    if (data & 0x3) == 3: # pointing west
        top = top.rotate(90)
        static_torch = (-3,3)
        torch = ((1,-4),(6,-1))


    def build_torch(active):
        # compose a "3d" redstone torch
        t = self.load_image_texture("assets/minecraft/textures/block/redstone_torch_off.png").copy() if not active else self.load_image_texture("assets/minecraft/textures/block/redstone_torch.png").copy()
        torch = Image.new("RGBA", (24,24), self.bgcolor)
        
        t_crop = t.crop((2,2,14,14))
        slice = t_crop.copy()
        ImageDraw.Draw(slice).rectangle((6,0,12,12),outline=(0,0,0,0),fill=(0,0,0,0))
        ImageDraw.Draw(slice).rectangle((0,0,4,12),outline=(0,0,0,0),fill=(0,0,0,0))
        
        alpha_over(torch, slice, (6,4))
        alpha_over(torch, t_crop, (5,5))
        alpha_over(torch, t_crop, (6,5))
        alpha_over(torch, slice, (6,6))

        return torch
    
    active_torch = build_torch(True)
    inactive_torch = build_torch(False)
    back_torch = active_torch if (blockid == 150 or data & 0b1000 == 0b1000) else inactive_torch
    static_torch_img = active_torch if (data & 0b100 == 0b100) else inactive_torch 

    img = self.build_full_block( (top, increment), None, None, side, side)

    alpha_over(img, static_torch_img, static_torch, static_torch_img) 
    alpha_over(img, back_torch, torch[0], back_torch) 
    alpha_over(img, back_torch, torch[1], back_torch) 
    return img
    
    
# trapdoor
# the trapdoor is looks like a sprite when opened, that's not good
@material(blockid=[96,167,451,11332,11333,11334,11335,11336,12501,12502], data=list(range(16)), transparent=True, nospawn=True)
def trapdoor(self, blockid, data):
    
    # texture generation
    model_map = {96:"oak_trapdoor",
            167:"iron_trapdoor",
            451:"mangrove_trapdoor",
            11332:"spruce_trapdoor",
            11333:"birch_trapdoor",
            11334:"jungle_trapdoor",
            11335:"acacia_trapdoor",
            11336:"dark_oak_trapdoor",
            12501:"crimson_trapdoor",
            12502:"warped_trapdoor",
            }
    facing = {0: 'north', 1: 'south', 2: 'west', 3: 'east'}[data % 4]
    
    if data & 0x4 == 0x4:  # off
        return self.build_block_from_model("%s_open" % model_map[blockid], {'facing': facing})
    if data & 0x8 == 0x8:  # off
        return self.build_block_from_model("%s_top" % model_map[blockid] )
    return self.build_block_from_model("%s_bottom" % model_map[blockid])

    # rotation
    # Masked to not clobber opened/closed info
    if self.rotation == 1:
        if (data & 0b0011) == 0: data = data & 0b1100 | 3
        elif (data & 0b0011) == 1: data = data & 0b1100 | 2
        elif (data & 0b0011) == 2: data = data & 0b1100 | 0
        elif (data & 0b0011) == 3: data = data & 0b1100 | 1
    elif self.rotation == 2:
        if (data & 0b0011) == 0: data = data & 0b1100 | 1
        elif (data & 0b0011) == 1: data = data & 0b1100 | 0
        elif (data & 0b0011) == 2: data = data & 0b1100 | 3
        elif (data & 0b0011) == 3: data = data & 0b1100 | 2
    elif self.rotation == 3:
        if (data & 0b0011) == 0: data = data & 0b1100 | 2
        elif (data & 0b0011) == 1: data = data & 0b1100 | 3
        elif (data & 0b0011) == 2: data = data & 0b1100 | 1
        elif (data & 0b0011) == 3: data = data & 0b1100 | 0

    # texture generation
    texturepath = {96:"assets/minecraft/textures/block/oak_trapdoor.png",
                   167:"assets/minecraft/textures/block/iron_trapdoor.png",
                   451:"assets/minecraft/textures/block/mangrove_trapdoor.png",
                   11332:"assets/minecraft/textures/block/spruce_trapdoor.png",
                   11333:"assets/minecraft/textures/block/birch_trapdoor.png",
                   11334:"assets/minecraft/textures/block/jungle_trapdoor.png",
                   11335:"assets/minecraft/textures/block/acacia_trapdoor.png",
                   11336:"assets/minecraft/textures/block/dark_oak_trapdoor.png",
                   12501:"assets/minecraft/textures/block/crimson_trapdoor.png",
                   12502:"assets/minecraft/textures/block/warped_trapdoor.png",
                  }[blockid]

    if data & 0x4 == 0x4: # opened trapdoor
        if data & 0x08 == 0x08: texture = self.load_image_texture(texturepath).transpose(Image.FLIP_TOP_BOTTOM)
        else: texture = self.load_image_texture(texturepath)

        if data & 0x3 == 0: # west
            img = self.build_full_block(None, None, None, None, texture)
        if data & 0x3 == 1: # east
            img = self.build_full_block(None, texture, None, None, None)
        if data & 0x3 == 2: # south
            img = self.build_full_block(None, None, texture, None, None)
        if data & 0x3 == 3: # north
            img = self.build_full_block(None, None, None, texture, None)

    elif data & 0x4 == 0: # closed trapdoor
        texture = self.load_image_texture(texturepath)
        if data & 0x8 == 0x8: # is a top trapdoor
            img = Image.new("RGBA", (24,24), self.bgcolor)
            t = self.build_full_block((texture, 12), None, None, texture, texture)
            alpha_over(img, t, (0,-9),t)
        else: # is a bottom trapdoor
            img = self.build_full_block((texture, 12), None, None, texture, texture)
    
    return img

# block with hidden silverfish (stone, cobblestone and stone brick)
@material(blockid=[4, 97, 98], data=list(range(3)), solid=True)
def hidden_silverfish(self, blockid, data):
    if blockid == 4:
        return self.build_block_from_model("cobblestone")
    if blockid == 97:
        if data == 0:
            return self.build_block_from_model("stone")
        if data == 1:
            return self.build_block_from_model("cobblestone")
        else:
            return self.build_block_from_model("stone_bricks")
    if blockid == 98:
        if data == 0:  # normal
            return self.build_block_from_model("stone_bricks")
        elif data == 1:  # mossy
            return self.build_block_from_model("mossy_stone_bricks")
        elif data == 2:  # cracked
            return self.build_block_from_model("cracked_stone_bricks")
        elif data == 3:  # "circle" stone brick
            return self.build_block_from_model("chiseled_stone_bricks")

@material(blockid=[99, 100, 139], data=list(range(64)), solid=True)
def huge_mushroom(self, blockid, data):
    # Re-arrange the bits in data based on self.rotation
    # rotation  bit: 654321
    #        0       DUENWS
    #        1       DUNWSE
    #        2       DUWSEN
    #        3       DUSENW
    if self.rotation in [1, 2, 3]:
        bit_map = {1: [6, 5, 3, 2, 1, 4],
                   2: [6, 5, 2, 1, 4, 3],
                   3: [6, 5, 1, 4, 3, 2]}
        new_data = 0


    # texture generation
    texture_map = {99:  "brown_mushroom_block",
                   100: "red_mushroom_block",
                   139: "mushroom_stem"}
    cap =  self.load_image_texture("assets/minecraft/textures/block/%s.png" % texture_map[blockid])
    porous = self.load_image_texture("assets/minecraft/textures/block/mushroom_block_inside.png")

    # Faces visible after amending data for rotation are: up, West, and South
    side_up    = cap if data & 0b010000 else porous  # Up
    side_west  = cap if data & 0b000010 else porous  # West
    side_south = cap if data & 0b000001 else porous  # South
    side_south = side_south.transpose(Image.FLIP_LEFT_RIGHT)

    return self.build_full_block(side_up, None, None, side_west, side_south)

# iron bars and glass pane
# TODO glass pane is not a sprite, it has a texture for the side,
# at the moment is not used
@material(blockid=[101,102, 160], data=list(range(256)), transparent=True, nospawn=True)
def panes(self, blockid, data):
    # no rotation, uses pseudo data
    if blockid == 101:
        # iron bars
        t = self.load_image_texture("assets/minecraft/textures/block/iron_bars.png")
    elif blockid == 160:
        t = self.load_image_texture("assets/minecraft/textures/block/%s_stained_glass.png" % color_map[data & 0xf])
    else:
        # glass panes
        t = self.load_image_texture("assets/minecraft/textures/block/glass.png")
    left = t.copy()
    right = t.copy()
    center = t.copy()

    # generate the four small pieces of the glass pane
    ImageDraw.Draw(right).rectangle((0,0,7,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(left).rectangle((8,0,15,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(center).rectangle((0,0,6,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(center).rectangle((9,0,15,15),outline=(0,0,0,0),fill=(0,0,0,0))

    up_center = self.transform_image_side(center)
    up_left = self.transform_image_side(left)
    up_right = self.transform_image_side(right).transpose(Image.FLIP_TOP_BOTTOM)
    dw_right = self.transform_image_side(right)
    dw_left = self.transform_image_side(left).transpose(Image.FLIP_TOP_BOTTOM)

    # Create img to compose the texture
    img = Image.new("RGBA", (24,24), self.bgcolor)

    # +x axis points top right direction
    # +y axis points bottom right direction
    # First compose things in the back of the image,
    # then things in the front.

    # the lower 4 bits encode color, the upper 4 encode adjencies
    data = data >> 4

    if data == 0:
        alpha_over(img, up_center, (6, 3), up_center) # center
    else:
        def draw_top_left():
            alpha_over(img, up_left, (6, 3), up_left)    # top left

        def draw_top_right():
            alpha_over(img, up_right, (6, 3), up_right)  # top right

        def draw_bottom_right():
            alpha_over(img, dw_right, (6, 3), dw_right)  # bottom right

        def draw_bottom_left():
            alpha_over(img, dw_left, (6, 3), dw_left)    # bottom left

        draw_funcs = [draw_top_left, draw_top_right, draw_bottom_right, draw_bottom_left]

        if (data & 0b0001) == 1:
            draw_funcs[(self.rotation + 0) % len(draw_funcs)]()
        if (data & 0b0010) == 2:
            draw_funcs[(self.rotation + 1) % len(draw_funcs)]()
        if (data & 0b0100) == 4:
            draw_funcs[(self.rotation + 2) % len(draw_funcs)]()
        if (data & 0b1000) == 8:
            draw_funcs[(self.rotation + 3) % len(draw_funcs)]()

    return img

# pumpkin and melon stem
# TODO To render it as in game needs from pseudo data and ancil data:
# once fully grown the stem bends to the melon/pumpkin block,
# at the moment only render the growing stem
@material(blockid=[104,105], data=list(range(8)), transparent=True)
def stem(self, blockid, data):
    # the ancildata value indicates how much of the texture
    # is shown.

    # not fully grown stem or no pumpkin/melon touching it,
    # straight up stem
    t = self.load_image_texture("assets/minecraft/textures/block/melon_stem.png").copy()
    img = Image.new("RGBA", (16,16), self.bgcolor)
    alpha_over(img, t, (0, int(16 - 16*((data + 1)/8.))), t)
    img = self.build_sprite(t)
    if data & 7 == 7:
        # fully grown stem gets brown color!
        # there is a conditional in rendermode-normal.c to not
        # tint the data value 7
        img = self.tint_texture(img, (211,169,116))
    return img


# nether vines
billboard(blockid=1012, imagename="assets/minecraft/textures/block/twisting_vines.png")
billboard(blockid=1013, imagename="assets/minecraft/textures/block/twisting_vines_plant.png")
billboard(blockid=1014, imagename="assets/minecraft/textures/block/weeping_vines.png")
billboard(blockid=1015, imagename="assets/minecraft/textures/block/weeping_vines_plant.png")

# vines
@material(blockid=106, data=list(range(32)), transparent=True, solid=False, nospawn=True)
def vines(self, blockid, data):
    # Re-arrange the bits in data based on self.rotation
    # rotation  bit: 54321
    #        0       UENWS
    #        1       UNWSE
    #        2       UWSEN
    #        3       USENW
    if self.rotation in [1, 2, 3]:
        bit_map = {1: [5, 3, 2, 1, 4],
                   2: [5, 2, 1, 4, 3],
                   3: [5, 1, 4, 3, 2]}
        new_data = 0

        # Add the ith bit to new_data then shift left one at a time,
        # re-ordering data's bits in the order specified in bit_map
        for i in bit_map[self.rotation]:
            new_data = new_data << 1
            new_data |= (data >> (i - 1)) & 1
        data = new_data

    # decode data and prepare textures
    raw_texture = self.load_image_texture("assets/minecraft/textures/block/vine.png")

    side_up    = raw_texture if data & 0b10000 else None  # Up
    side_east  = raw_texture if data & 0b01000 else None  # East
    side_north = raw_texture if data & 0b00100 else None  # North
    side_west  = raw_texture if data & 0b00010 else None  # West
    side_south = raw_texture if data & 0b00001 else None  # South

    return self.build_full_block(side_up, side_north, side_east, side_west, side_south)


# fence gates
@material(blockid=[107, 183, 184, 185, 186, 187, 455, 513, 514], data=list(range(8)), transparent=True, nospawn=True)
def fence_gate(self, blockid, data):

    # rotation
    opened = False
    if data & 0x4:
        data = data & 0x3
        opened = True
    if self.rotation == 1:
        if data == 0: data = 1
        elif data == 1: data = 2
        elif data == 2: data = 3
        elif data == 3: data = 0
    elif self.rotation == 2:
        if data == 0: data = 2
        elif data == 1: data = 3
        elif data == 2: data = 0
        elif data == 3: data = 1
    elif self.rotation == 3:
        if data == 0: data = 3
        elif data == 1: data = 0
        elif data == 2: data = 1
        elif data == 3: data = 2
    if opened:
        data = data | 0x4

    # create the closed gate side
    if blockid == 107: # Oak
        gate_side = self.load_image_texture("assets/minecraft/textures/block/oak_planks.png").copy()
    elif blockid == 183: # Spruce
        gate_side = self.load_image_texture("assets/minecraft/textures/block/spruce_planks.png").copy()
    elif blockid == 184: # Birch
        gate_side = self.load_image_texture("assets/minecraft/textures/block/birch_planks.png").copy()
    elif blockid == 185: # Jungle
        gate_side = self.load_image_texture("assets/minecraft/textures/block/jungle_planks.png").copy()
    elif blockid == 186: # Dark Oak
        gate_side = self.load_image_texture("assets/minecraft/textures/block/dark_oak_planks.png").copy()
    elif blockid == 187: # Acacia
        gate_side = self.load_image_texture("assets/minecraft/textures/block/acacia_planks.png").copy()
    elif blockid == 455: # Mangrove
        gate_side = self.load_image_texture("assets/minecraft/textures/block/mangrove_planks.png").copy()
    elif blockid == 513: # Crimson
        gate_side = self.load_image_texture("assets/minecraft/textures/block/crimson_planks.png").copy()
    elif blockid == 514: # Warped
        gate_side = self.load_image_texture("assets/minecraft/textures/block/warped_planks.png").copy()
    else:
        return None

    gate_side_draw = ImageDraw.Draw(gate_side)
    gate_side_draw.rectangle((7,0,15,0),outline=(0,0,0,0),fill=(0,0,0,0))
    gate_side_draw.rectangle((7,4,9,6),outline=(0,0,0,0),fill=(0,0,0,0))
    gate_side_draw.rectangle((7,10,15,16),outline=(0,0,0,0),fill=(0,0,0,0))
    gate_side_draw.rectangle((0,12,15,16),outline=(0,0,0,0),fill=(0,0,0,0))
    gate_side_draw.rectangle((0,0,4,15),outline=(0,0,0,0),fill=(0,0,0,0))
    gate_side_draw.rectangle((14,0,15,15),outline=(0,0,0,0),fill=(0,0,0,0))
    
    # darken the sides slightly, as with the fences
    sidealpha = gate_side.split()[3]
    gate_side = ImageEnhance.Brightness(gate_side).enhance(0.9)
    gate_side.putalpha(sidealpha)
    
    # create the other sides
    mirror_gate_side = self.transform_image_side(gate_side.transpose(Image.FLIP_LEFT_RIGHT))
    gate_side = self.transform_image_side(gate_side)
    gate_other_side = gate_side.transpose(Image.FLIP_LEFT_RIGHT)
    mirror_gate_other_side = mirror_gate_side.transpose(Image.FLIP_LEFT_RIGHT)
    
    # Create img to compose the fence gate
    img = Image.new("RGBA", (24,24), self.bgcolor)
    
    if data & 0x4:
        # opened
        data = data & 0x3
        if data == 0:
            alpha_over(img, gate_side, (2,8), gate_side)
            alpha_over(img, gate_side, (13,3), gate_side)
        elif data == 1:
            alpha_over(img, gate_other_side, (-1,3), gate_other_side)
            alpha_over(img, gate_other_side, (10,8), gate_other_side)
        elif data == 2:
            alpha_over(img, mirror_gate_side, (-1,7), mirror_gate_side)
            alpha_over(img, mirror_gate_side, (10,2), mirror_gate_side)
        elif data == 3:
            alpha_over(img, mirror_gate_other_side, (2,1), mirror_gate_other_side)
            alpha_over(img, mirror_gate_other_side, (13,7), mirror_gate_other_side)
    else:
        # closed
        
        # positions for pasting the fence sides, as with fences
        pos_top_left = (2,3)
        pos_top_right = (10,3)
        pos_bottom_right = (10,7)
        pos_bottom_left = (2,7)
        
        if data == 0 or data == 2:
            alpha_over(img, gate_other_side, pos_top_right, gate_other_side)
            alpha_over(img, mirror_gate_other_side, pos_bottom_left, mirror_gate_other_side)
        elif data == 1 or data == 3:
            alpha_over(img, gate_side, pos_top_left, gate_side)
            alpha_over(img, mirror_gate_side, pos_bottom_right, mirror_gate_side)
    
    return img

# lilypad
# At the moment of writing this lilypads has no ancil data and their
# orientation depends on their position on the map. So it uses pseudo
# ancildata.
@material(blockid=111, data=list(range(4)), transparent=True)
def lilypad(self, blockid, data):
    t = self.load_image_texture("assets/minecraft/textures/block/lily_pad.png").copy()
    if data == 0:
        t = t.rotate(180)
    elif data == 1:
        t = t.rotate(270)
    elif data == 2:
        t = t
    elif data == 3:
        t = t.rotate(90)

    return self.build_full_block(None, None, None, None, None, t)

# nether wart
@material(blockid=115, data=list(range(4)), transparent=True)
def nether_wart(self, blockid, data):
    if data == 0: # just come up
        t = self.load_image_texture("assets/minecraft/textures/block/nether_wart_stage0.png")
    elif data in (1, 2):
        t = self.load_image_texture("assets/minecraft/textures/block/nether_wart_stage1.png")
    else: # fully grown
        t = self.load_image_texture("assets/minecraft/textures/block/nether_wart_stage2.png")
    
    # use the same technic as tall grass
    img = self.build_billboard(t)

    return img

# enchantment table
# there's no book at the moment because it is not a part of the model
@material(blockid=116, transparent=True)
def enchantment_table(self, blockid, data):
    return self.build_block_from_model('enchanting_table')

# brewing stand
# TODO this is a place holder, is a 2d image pasted
@material(blockid=117, data=list(range(5)), transparent=True)
def brewing_stand(self, blockid, data):
    base = self.load_image_texture("assets/minecraft/textures/block/brewing_stand_base.png")
    img = self.build_full_block(None, None, None, None, None, base)
    t = self.load_image_texture("assets/minecraft/textures/block/brewing_stand.png")
    stand = self.build_billboard(t)
    alpha_over(img,stand,(0,-2))
    return img


# cauldron
@material(blockid=118, data=list(range(16)), transparent=True, solid=True, nospawn=True)
def cauldron(self, blockid, data):
    side = self.load_image_texture("assets/minecraft/textures/block/cauldron_side.png").copy()
    top = self.load_image_texture("assets/minecraft/textures/block/cauldron_top.png")
    filltype = (data & (3 << 2)) >> 2
    if filltype == 3:
        water = self.transform_image_top(self.load_image_texture("assets/minecraft/textures/block/powder_snow.png"))
    elif filltype == 2:
        water = self.transform_image_top(self.load_image_texture("assets/minecraft/textures/block/lava_still.png"))
    else: # filltype == 1 or 0
        water = self.transform_image_top(self.load_image_texture("water.png"))
    # Side texture isn't transparent between the feet, so adjust the texture
    ImageDraw.Draw(side).rectangle((5, 14, 11, 16), outline=(0, 0, 0, 0), fill=(0, 0, 0, 0))

    level = (data & 3)
    if level == 0:  # Empty
        img = self.build_full_block(top, side, side, side, side)
    else:  # Part or fully filled
        # Is filled in increments of a third, with level indicating how many thirds are filled
        img = self.build_full_block(None, side, side, None, None)
        alpha_over(img, water, (0, 12 - level * 4), water)
        img2 = self.build_full_block(top, None, None, side, side)
        alpha_over(img, img2, (0, 0), img2)
    return img


# end portal and end_gateway
@material(blockid=[119,209], transparent=True)
def end_portal(self, blockid, data):
    img = Image.new("RGBA", (24,24), self.bgcolor)
    # generate a black texure with white, blue and grey dots resembling stars
    t = Image.new("RGBA", (16,16), (0,0,0,255))
    for color in [(155,155,155,255), (100,255,100,255), (255,255,255,255)]:
        for i in range(6):
            x = randint(0,15)
            y = randint(0,15)
            t.putpixel((x,y),color)
    if blockid == 209: # end_gateway
        return  self.build_block(t, t)
        
    t = self.transform_image_top(t)
    alpha_over(img, t, (0,6), t)

    return img


# end portal frame (data range 8 to get all orientations of filled)
@material(blockid=120, data=list(range(8)), transparent=True, solid=True, nospawn=True)
def end_portal_frame(self, blockid, data):
    facing = {2: 'north', 0: 'south', 1: 'west', 3: 'east'}[data % 4]
    if data & 0x4 == 0x4:
        return self.build_block_from_model('end_portal_frame_filled', {'facing': facing})
    return self.build_block_from_model('end_portal_frame', {'facing': facing})

@material(blockid=[123], data=list(range(2)), solid=True)
def redstone_lamp(self, blockid, data):
    if data == 0:  # off
        return self.build_block_from_model('redstone_lamp')
    return self.build_block_from_model('redstone_lamp_on')
    
# daylight sensor.  
@material(blockid=[151,178], transparent=True)
def daylight_sensor(self, blockid, data):
    if blockid == 151: # daylight sensor
        return self.build_block_from_model('daylight_detector')
    else: # inverted daylight sensor
        return self.build_block_from_model('daylight_detector_inverted')


# wooden double and normal slabs
# these are the new wooden slabs, blockids 43 44 still have wooden
# slabs, but those are unobtainable without cheating
@material(blockid=[125, 126], data=list(range(16)), transparent=(44,), solid=True)
def wooden_slabs(self, blockid, data):
    texture = data & 7
    if texture== 0: # oak 
        top = side = self.load_image_texture("assets/minecraft/textures/block/oak_planks.png")
    elif texture== 1: # spruce
        top = side = self.load_image_texture("assets/minecraft/textures/block/spruce_planks.png")
    elif texture== 2: # birch
        top = side = self.load_image_texture("assets/minecraft/textures/block/birch_planks.png")
    elif texture== 3: # jungle
        top = side = self.load_image_texture("assets/minecraft/textures/block/jungle_planks.png")
    elif texture== 4: # acacia
        top = side = self.load_image_texture("assets/minecraft/textures/block/acacia_planks.png")
    elif texture== 5: # dark wood
        top = side = self.load_image_texture("assets/minecraft/textures/block/dark_oak_planks.png")
    elif texture== 6: # crimson
        top = side = self.load_image_texture("assets/minecraft/textures/block/crimson_planks.png")
    elif texture== 7: # warped
        top = side = self.load_image_texture("assets/minecraft/textures/block/warped_planks.png")
    else:
        return None
    
    if blockid == 125: # double slab
        return self.build_block(top, side)
    
    return self.build_slab_block(top, side, data & 8 == 8);

# mineral overlay
# emerald ore
solidmodelblock(blockid=129, name="emerald_ore")

# cocoa plant
@material(blockid=127, data=list(range(12)), transparent=True)
def cocoa_plant(self, blockid, data):
    orientation = data & 3
    # rotation
    if self.rotation == 1:
        if orientation == 0: orientation = 1
        elif orientation == 1: orientation = 2
        elif orientation == 2: orientation = 3
        elif orientation == 3: orientation = 0
    elif self.rotation == 2:
        if orientation == 0: orientation = 2
        elif orientation == 1: orientation = 3
        elif orientation == 2: orientation = 0
        elif orientation == 3: orientation = 1
    elif self.rotation == 3:
        if orientation == 0: orientation = 3
        elif orientation == 1: orientation = 0
        elif orientation == 2: orientation = 1
        elif orientation == 3: orientation = 2

    size = data & 12
    if size == 8: # big
        t = self.load_image_texture("assets/minecraft/textures/block/cocoa_stage2.png")
        c_left = (0,3)
        c_right = (8,3)
        c_top = (5,2)
    elif size == 4: # normal
        t = self.load_image_texture("assets/minecraft/textures/block/cocoa_stage1.png")
        c_left = (-2,2)
        c_right = (8,2)
        c_top = (5,2)
    elif size == 0: # small
        t = self.load_image_texture("assets/minecraft/textures/block/cocoa_stage0.png")
        c_left = (-3,2)
        c_right = (6,2)
        c_top = (5,2)

    # let's get every texture piece necessary to do this
    stalk = t.copy()
    ImageDraw.Draw(stalk).rectangle((0,0,11,16),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(stalk).rectangle((12,4,16,16),outline=(0,0,0,0),fill=(0,0,0,0))
    
    top = t.copy() # warning! changes with plant size
    ImageDraw.Draw(top).rectangle((0,7,16,16),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(top).rectangle((7,0,16,6),outline=(0,0,0,0),fill=(0,0,0,0))

    side = t.copy() # warning! changes with plant size
    ImageDraw.Draw(side).rectangle((0,0,6,16),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(side).rectangle((0,0,16,3),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(side).rectangle((0,14,16,16),outline=(0,0,0,0),fill=(0,0,0,0))
    
    # first compose the block of the cocoa plant
    block = Image.new("RGBA", (24,24), self.bgcolor)
    tmp = self.transform_image_side(side).transpose(Image.FLIP_LEFT_RIGHT)
    alpha_over (block, tmp, c_right,tmp) # right side
    tmp = tmp.transpose(Image.FLIP_LEFT_RIGHT)
    alpha_over (block, tmp, c_left,tmp) # left side
    tmp = self.transform_image_top(top)
    alpha_over(block, tmp, c_top,tmp)
    if size == 0:
        # fix a pixel hole
        block.putpixel((6,9), block.getpixel((6,10)))

    # compose the cocoa plant
    img = Image.new("RGBA", (24,24), self.bgcolor)
    if orientation in (2,3): # south and west
        tmp = self.transform_image_side(stalk).transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, block,(-1,-2), block)
        alpha_over(img, tmp, (4,-2), tmp)
        if orientation == 3:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
    elif orientation in (0,1): # north and east
        tmp = self.transform_image_side(stalk.transpose(Image.FLIP_LEFT_RIGHT))
        alpha_over(img, block,(-1,5), block)
        alpha_over(img, tmp, (2,12), tmp)
        if orientation == 0:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)

    return img

# cobblestone and mossy cobblestone walls, chorus plants, mossy stone brick walls
# one additional bit of data value added for mossy and cobblestone
@material(blockid=[199]+list(range(1792, 1813 + 1)), data=list(range(32)), transparent=True, nospawn=True)
def cobblestone_wall(self, blockid, data):
    walls_id_to_tex = {
          199: "assets/minecraft/textures/block/chorus_plant.png", # chorus plants
        1792: "assets/minecraft/textures/block/andesite.png",
        1793: "assets/minecraft/textures/block/bricks.png",
        1794: "assets/minecraft/textures/block/cobblestone.png",
        1795: "assets/minecraft/textures/block/diorite.png",
        1796: "assets/minecraft/textures/block/end_stone_bricks.png",
        1797: "assets/minecraft/textures/block/granite.png",
        1798: "assets/minecraft/textures/block/mossy_cobblestone.png",
        1799: "assets/minecraft/textures/block/mossy_stone_bricks.png",
        1800: "assets/minecraft/textures/block/nether_bricks.png",
        1801: "assets/minecraft/textures/block/prismarine.png",
        1802: "assets/minecraft/textures/block/red_nether_bricks.png",
        1803: "assets/minecraft/textures/block/red_sandstone.png",
        1804: "assets/minecraft/textures/block/sandstone.png",
        1805: "assets/minecraft/textures/block/stone_bricks.png",
        1806: "assets/minecraft/textures/block/blackstone.png",
        1807: "assets/minecraft/textures/block/polished_blackstone.png",
        1808: "assets/minecraft/textures/block/polished_blackstone_bricks.png",
        1809: "assets/minecraft/textures/block/cobbled_deepslate.png",
        1810: "assets/minecraft/textures/block/polished_deepslate.png",
        1811: "assets/minecraft/textures/block/deepslate_bricks.png",
        1812: "assets/minecraft/textures/block/deepslate_tiles.png",
        1813: "assets/minecraft/textures/block/mud_bricks.png",
    }
    t = self.load_image_texture(walls_id_to_tex[blockid]).copy()

    wall_pole_top = t.copy()
    wall_pole_side = t.copy()
    wall_side_top = t.copy()
    wall_side = t.copy()
    # _full is used for walls without pole
    wall_side_top_full = t.copy()
    wall_side_full = t.copy()

    # generate the textures of the wall
    ImageDraw.Draw(wall_pole_top).rectangle((0,0,3,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(wall_pole_top).rectangle((12,0,15,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(wall_pole_top).rectangle((0,0,15,3),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(wall_pole_top).rectangle((0,12,15,15),outline=(0,0,0,0),fill=(0,0,0,0))

    ImageDraw.Draw(wall_pole_side).rectangle((0,0,3,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(wall_pole_side).rectangle((12,0,15,15),outline=(0,0,0,0),fill=(0,0,0,0))

    # Create the sides and the top of the pole
    wall_pole_side = self.transform_image_side(wall_pole_side)
    wall_pole_other_side = wall_pole_side.transpose(Image.FLIP_LEFT_RIGHT)
    wall_pole_top = self.transform_image_top(wall_pole_top)

    # Darken the sides slightly. These methods also affect the alpha layer,
    # so save them first (we don't want to "darken" the alpha layer making
    # the block transparent)
    sidealpha = wall_pole_side.split()[3]
    wall_pole_side = ImageEnhance.Brightness(wall_pole_side).enhance(0.8)
    wall_pole_side.putalpha(sidealpha)
    othersidealpha = wall_pole_other_side.split()[3]
    wall_pole_other_side = ImageEnhance.Brightness(wall_pole_other_side).enhance(0.7)
    wall_pole_other_side.putalpha(othersidealpha)

    # Compose the wall pole
    wall_pole = Image.new("RGBA", (24,24), self.bgcolor)
    alpha_over(wall_pole,wall_pole_side, (3,4),wall_pole_side)
    alpha_over(wall_pole,wall_pole_other_side, (9,4),wall_pole_other_side)
    alpha_over(wall_pole,wall_pole_top, (0,0),wall_pole_top)

    # create the sides and the top of a wall attached to a pole
    ImageDraw.Draw(wall_side).rectangle((0,0,15,2),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(wall_side).rectangle((0,0,11,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(wall_side_top).rectangle((0,0,11,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(wall_side_top).rectangle((0,0,15,4),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(wall_side_top).rectangle((0,11,15,15),outline=(0,0,0,0),fill=(0,0,0,0))
    # full version, without pole
    ImageDraw.Draw(wall_side_full).rectangle((0,0,15,2),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(wall_side_top_full).rectangle((0,4,15,15),outline=(0,0,0,0),fill=(0,0,0,0))
    ImageDraw.Draw(wall_side_top_full).rectangle((0,4,15,15),outline=(0,0,0,0),fill=(0,0,0,0))

    # compose the sides of a wall atached to a pole
    tmp = Image.new("RGBA", (24,24), self.bgcolor)
    wall_side = self.transform_image_side(wall_side)
    wall_side_top = self.transform_image_top(wall_side_top)

    # Darken the sides slightly. These methods also affect the alpha layer,
    # so save them first (we don't want to "darken" the alpha layer making
    # the block transparent)
    sidealpha = wall_side.split()[3]
    wall_side = ImageEnhance.Brightness(wall_side).enhance(0.7)
    wall_side.putalpha(sidealpha)

    alpha_over(tmp,wall_side, (0,0),wall_side)
    alpha_over(tmp,wall_side_top, (-5,3),wall_side_top)
    wall_side = tmp
    wall_other_side = wall_side.transpose(Image.FLIP_LEFT_RIGHT)

    # compose the sides of the full wall
    tmp = Image.new("RGBA", (24,24), self.bgcolor)
    wall_side_full = self.transform_image_side(wall_side_full)
    wall_side_top_full = self.transform_image_top(wall_side_top_full.rotate(90))

    # Darken the sides slightly. These methods also affect the alpha layer,
    # so save them first (we don't want to "darken" the alpha layer making
    # the block transparent)
    sidealpha = wall_side_full.split()[3]
    wall_side_full = ImageEnhance.Brightness(wall_side_full).enhance(0.7)
    wall_side_full.putalpha(sidealpha)

    alpha_over(tmp,wall_side_full, (4,0),wall_side_full)
    alpha_over(tmp,wall_side_top_full, (3,-4),wall_side_top_full)
    wall_side_full = tmp
    wall_other_side_full = wall_side_full.transpose(Image.FLIP_LEFT_RIGHT)

    # Create img to compose the wall
    img = Image.new("RGBA", (24,24), self.bgcolor)

    # Position wall imgs around the wall bit stick
    pos_top_left = (-5,-2)
    pos_bottom_left = (-8,4)
    pos_top_right = (5,-3)
    pos_bottom_right = (7,4)
    
    # +x axis points top right direction
    # +y axis points bottom right direction
    # There are two special cases for wall without pole.
    # Normal case: 
    # First compose the walls in the back of the image, 
    # then the pole and then the walls in the front.
    if (data == 0b1010) or (data == 0b11010):
        alpha_over(img, wall_other_side_full,(0,2), wall_other_side_full)
    elif (data == 0b0101) or (data == 0b10101):
        alpha_over(img, wall_side_full,(0,2), wall_side_full)
    else:
        if (data & 0b0001) == 1:
            alpha_over(img,wall_side, pos_top_left,wall_side)                # top left
        if (data & 0b1000) == 8:
            alpha_over(img,wall_other_side, pos_top_right,wall_other_side)    # top right

        alpha_over(img,wall_pole,(0,0),wall_pole)
            
        if (data & 0b0010) == 2:
            alpha_over(img,wall_other_side, pos_bottom_left,wall_other_side)      # bottom left    
        if (data & 0b0100) == 4:
            alpha_over(img,wall_side, pos_bottom_right,wall_side)                  # bottom right
    
    return img

# carrots, potatoes
@material(blockid=[141,142], data=list(range(8)), transparent=True, nospawn=True)
def crops4(self, blockid, data):
    # carrots and potatoes have 8 data, but only 4 visual stages
    stage = {0:0,
             1:0,
             2:1,
             3:1,
             4:2,
             5:2,
             6:2,
             7:3}[data]
    if blockid == 141: # carrots
        raw_crop = self.load_image_texture("assets/minecraft/textures/block/carrots_stage%d.png" % stage)
    else: # potatoes
        raw_crop = self.load_image_texture("assets/minecraft/textures/block/potatoes_stage%d.png" % stage)
    crop1 = self.transform_image_top(raw_crop)
    crop2 = self.transform_image_side(raw_crop)
    crop3 = crop2.transpose(Image.FLIP_LEFT_RIGHT)

    img = Image.new("RGBA", (24,24), self.bgcolor)
    alpha_over(img, crop1, (0,12), crop1)
    alpha_over(img, crop2, (6,3), crop2)
    alpha_over(img, crop3, (6,3), crop3)
    return img


@material(blockid=145, data=list(range(12)), transparent=True, nospawn=True)
def anvil(self, blockid, data):
    facing = {2: 'north', 0: 'south', 1: 'west', 3: 'east'}[data % 4]
    if (data & 0xc) == 0:  # non damaged anvil
        return self.build_block_from_model('anvil', {'facing': facing})
    elif (data & 0xc) == 0x4:  # slightly damaged
        return self.build_block_from_model('chipped_anvil', {'facing': facing})
    elif (data & 0xc) == 0x8:  # very damaged
        return self.build_block_from_model('damaged_anvil', {'facing': facing})

# mineral overlay
# nether quartz ore
solidmodelblock(blockid=153, name="nether_quartz_ore")

# block of quartz
@material(blockid=155, data=list(range(3)), solid=True)
def quartz_pillar(self, blockid, data):
    return self.build_block_from_model('quartz_pillar', blockstate={'axis': ({0: 'y', 1: 'x', 2: 'z'}[data])})
    
# hopper
@material(blockid=154, data=list(range(6)), transparent=True)
def hopper(self, blockid, data):
    # # TODO: 
    # facing = {0: 'down', 1: 'up', 2: 'east', 3: 'south', 4: 'west', 5: 'north'}[data]
    # if facing == 'down':
    #     return self.build_block_from_model('hopper', {'facing': facing})
    # return self.build_block_from_model('hopper_side', {'facing': facing})


    #build the top
    side = self.load_image_texture("assets/minecraft/textures/block/hopper_outside.png")
    top = self.load_image_texture("assets/minecraft/textures/block/hopper_top.png")
    bottom = self.load_image_texture("assets/minecraft/textures/block/hopper_inside.png")
    hop_top = self.build_full_block((top,10), side, side, side, side, side)

    #build a solid block for mid/top
    hop_mid = self.build_full_block((top,5), side, side, side, side, side)
    hop_bot = self.build_block(side,side)

    hop_mid = hop_mid.resize((17,17),Image.ANTIALIAS)
    hop_bot = hop_bot.resize((10,10),Image.ANTIALIAS)
    
    #compose the final block
    img = Image.new("RGBA", (24,24), self.bgcolor)
    alpha_over(img, hop_bot, (7,14), hop_bot)
    alpha_over(img, hop_mid, (3,3), hop_mid)
    alpha_over(img, hop_top, (0,-6), hop_top)

    return img

# hay block
@material(blockid=170, data=list(range(3)), solid=True)
def hayblock(self, blockid, data):
    return self.build_block_from_model('hay_block', blockstate={'axis': ({0: 'y', 1: 'x', 2: 'z'}[data])})

@material(blockid=175, data=list(range(16)), transparent=True)
def flower(self, blockid, data):
    double_plant_map = ["sunflower", "lilac", "tall_grass", "large_fern", "rose_bush", "peony", "peony", "peony"]
    plant = double_plant_map[data & 0x7]

    if data & 0x8:
        part = "top"
    else:
        part = "bottom"

    png = "assets/minecraft/textures/block/%s_%s.png" % (plant,part)
    texture = self.load_image_texture(png)
    img = self.build_billboard(texture)

    #sunflower top
    if data == 8:
        bloom_tex = self.load_image_texture("assets/minecraft/textures/block/sunflower_front.png")
        alpha_over(img, bloom_tex.resize((14, 11), Image.ANTIALIAS), (5,5))

    return img

# chorus flower
@material(blockid=200, data=list(range(6)), solid=True)
def chorus_flower(self, blockid, data):
    # aged 5, dead
    if data == 5:
        return self.build_block_from_model("chorus_flower_dead")
    else:
        return self.build_block_from_model("chorus_flower")

# purpur pillar
@material(blockid=202, data=list(range(3)), solid=True)
def purpur_pillar(self, blockid, data):
    return self.build_block_from_model('purpur_pillar', blockstate={'axis': ({0: 'y', 1: 'x', 2: 'z'}[data])})

# frosted ice
@material(blockid=212, data=list(range(4)), solid=True)
def frosted_ice(self, blockid, data):
    return self.build_block_from_model("frosted_ice_%d" % data)


@material(blockid=216, data=list(range(12)), solid=True)
def boneblock(self, blockid, data):
    return self.build_block_from_model('bone_block', blockstate={'axis': ({0: 'y', 4: 'x', 8: 'z'}[data & 12])})

# observer
@material(blockid=218, data=[0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13], solid=True, nospawn=True)
def observer(self, blockid, data):
    facing = {0: 'down', 1: 'up', 2: 'north', 3: 'south', 4: 'west', 5: 'east'}[data & 0b0111]
    if data & 0b1000:
        return self.build_block_from_model('observer', {'facing': facing})
    else:
        return self.build_block_from_model('observer_on', {'facing': facing})

# shulker box
@material(blockid=list(range(219, 235)) + [257], data=list(range(6)), solid=True, nospawn=True)
def shulker_box(self, blockid, data):
    # Do rotation
    if self.rotation in [1, 2, 3] and data in [2, 3, 4, 5]:
        rotation_map = {1: {2: 5, 3: 4, 4: 2, 5: 3},
                        2: {2: 3, 3: 2, 4: 5, 5: 4},
                        3: {2: 4, 3: 5, 4: 3, 5: 2}}
        data = rotation_map[self.rotation][data]

    if blockid == 257:
        # Uncolored shulker box
        file_name = "shulker.png"
    else:
        file_name = "shulker_%s.png" % color_map[blockid - 219]

    shulker_t = self.load_image("assets/minecraft/textures/entity/shulker/%s" % file_name).copy()
    w, h = shulker_t.size
    res = w // 4
    # Cut out the parts of the shulker texture we need for the box
    top = shulker_t.crop((res, 0, res * 2, res))
    bottom = shulker_t.crop((res * 2, int(res * 1.75), res * 3, int(res * 2.75)))
    side_top = shulker_t.crop((0, res, res, int(res * 1.75)))
    side_bottom = shulker_t.crop((0, int(res * 2.75), res, int(res * 3.25)))
    side = Image.new('RGBA', (res, res))
    side.paste(side_top, (0, 0), side_top)
    side.paste(side_bottom, (0, res // 2), side_bottom)

    if data == 0:    # down
        side = side.rotate(180)
        img = self.build_full_block(bottom, None, None, side, side)
    elif data == 1:  # up
        img = self.build_full_block(top, None, None, side, side)
    elif data == 2:  # east
        img = self.build_full_block(side, None, None, side.rotate(90), bottom)
    elif data == 3:  # west
        img = self.build_full_block(side.rotate(180), None, None, side.rotate(270), top)
    elif data == 4:  # north
        img = self.build_full_block(side.rotate(90), None, None, top, side.rotate(270))
    elif data == 5:  # south
        img = self.build_full_block(side.rotate(270), None, None, bottom, side.rotate(90))

    return img


# structure block
@material(blockid=255, data=list(range(4)), solid=True)
def structure_block(self, blockid, data):
    if data == 0:
        return self.build_block_from_model("structure_block_save")
    elif data == 1:
        return self.build_block_from_model("structure_block_load")
    elif data == 2:
        return self.build_block_from_model("structure_block_corner")
    elif data == 3:
        return self.build_block_from_model("structure_block_data")
    else:
        raise Exception('unexpected structure block: ' + str(data))

# Jigsaw block
@material(blockid=256, data=list(range(6)), solid=True)
def jigsaw_block(self, blockid, data):
    facing = {0: 'down', 1: 'up', 2: 'north', 3: 'south', 4: 'west', 5: 'east'}[data]
    return self.build_block_from_model('jigsaw', {'facing':facing})


# beetroots(207), berry bushes (11505)
@material(blockid=[207, 11505], data=list(range(4)), transparent=True, nospawn=True)
def crops(self, blockid, data):
    if blockid == 207:
        return self.build_block_from_model("beetroots_stage%d" % data)
    else:
        raw_crop = self.load_image_texture("assets/minecraft/textures/block/sweet_berry_bush_stage%d.png" % data)
    crop1 = self.transform_image_top(raw_crop)
    crop2 = self.transform_image_side(raw_crop)
    crop3 = crop2.transpose(Image.FLIP_LEFT_RIGHT)

    img = Image.new("RGBA", (24,24), self.bgcolor)
    alpha_over(img, crop1, (0,12), crop1)
    alpha_over(img, crop2, (6,3), crop2)
    alpha_over(img, crop3, (6,3), crop3)
    return img

# Glazed Terracotta
@material(blockid=list(range(235, 251)), data=list(range(4)), solid=True)
def glazed_terracotta(self, blockid, data):
    facing = {0: 'south', 1: 'west', 2: 'north', 3: 'east'}[data]
    return self.build_block_from_model("%s_glazed_terracotta" % color_map[blockid - 235], {'facing': facing})

# scaffolding
@material(blockid=[11414], data=list(range(2)), solid=False, transparent=True)
def scaffolding(self, blockid, data):
    top = self.load_image_texture("assets/minecraft/textures/block/scaffolding_top.png")
    side = self.load_image_texture("assets/minecraft/textures/block/scaffolding_side.png")    
    img = self.build_block(top, side)
    return img

# beehive and bee_nest
@material(blockid=[11501, 11502], data=list(range(8)), solid=True)
def beehivenest(self, blockid, data):    
    facing = {0: 'south', 1: 'west', 2: 'north', 3: 'east'}[data % 4]
    if blockid == 11501:
        if data >= 4:
            return self.build_block_from_model('beehive_honey', {'facing': facing})
        return self.build_block_from_model('beehive', {'facing': facing})
    else:  # blockid == 11502:
        if data >= 4:
            return self.build_block_from_model('bee_nest_honey', {'facing': facing})
        return self.build_block_from_model('bee_nest', {'facing': facing})

# Barrel
@material(blockid=11418, data=list(range(12)), solid=True)
def barrel(self, blockid, data):
    facing = {0: 'up', 1: 'down', 2: 'south', 3: 'east', 4: 'north', 5: 'west'}[data >> 1]

    if data & 0x01:
        return self.build_block_from_model('barrel_open', {'facing': facing})
    return self.build_block_from_model('barrel', {'facing': facing})

# Campfire (11506) and soul campfire (1003)
@material(blockid=[11506, 1003], data=list(range(8)), solid=True, transparent=True, nospawn=True)
def campfire(self, blockid, data):
    # Do rotation, mask to not clobber lit data
    data = data & 0b100 | ((self.rotation + (data & 0b11)) % 4)
    block_name = "campfire" if blockid == 11506 else "soul_campfire"

    # Load textures
    # Fire & lit log textures contain multiple tiles, since both are
    #   16px wide rely on load_image_texture() to crop appropriately
    fire_raw_t = self.load_image_texture("assets/minecraft/textures/block/" + block_name
                                         + "_fire.png")
    log_raw_t = self.load_image_texture("assets/minecraft/textures/block/campfire_log.png")
    log_lit_raw_t = self.load_image_texture("assets/minecraft/textures/block/" + block_name
                                            + "_log_lit.png")

    def create_tile(img_src, coord_crop, coord_paste, rot):
        # Takes an image, crops a region, optionally rotates the
        #   texture, then finally pastes it onto a 16x16 image
        img_out = Image.new("RGBA", (16, 16), self.bgcolor)
        img_in = img_src.crop(coord_crop)
        if rot != 0:
            img_in = img_in.rotate(rot, expand=True)
        img_out.paste(img_in, coord_paste)
        return img_out

    # Generate bottom
    bottom_t = log_lit_raw_t if data & 0b100 else log_raw_t
    bottom_t = create_tile(bottom_t, (0, 8, 16, 14), (0, 5), 0)
    bottom_t = self.transform_image_top(bottom_t)

    # Generate two variants of a log: one with a lit side, one without
    log_t = Image.new("RGBA", (24, 24), self.bgcolor)
    log_end_t = create_tile(log_raw_t, (0, 4, 4, 8), (12, 6), 0)
    log_side_t = create_tile(log_raw_t, (0, 0, 16, 4), (0, 6), 0)
    log_side_lit_t = create_tile(log_lit_raw_t, (0, 0, 16, 4), (0, 6), 0)

    log_end_t = self.transform_image_side(log_end_t)
    log_top_t = self.transform_image_top(log_side_t)
    log_side_t = self.transform_image_side(log_side_t).transpose(Image.FLIP_LEFT_RIGHT)
    log_side_lit_t = self.transform_image_side(log_side_lit_t).transpose(Image.FLIP_LEFT_RIGHT)

    alpha_over(log_t, log_top_t, (-2, 2), log_top_t)  # Fix some holes at the edges
    alpha_over(log_t, log_top_t, (-2, 1), log_top_t)
    log_lit_t = log_t.copy()

    # Unlit log
    alpha_over(log_t, log_side_t, (5, 0), log_side_t)
    alpha_over(log_t, log_end_t, (-7, 0), log_end_t)

    # Lit log. For unlit fires, just reference the unlit log texture
    if data & 0b100:
        alpha_over(log_lit_t, log_side_lit_t, (5, 0), log_side_lit_t)
        alpha_over(log_lit_t, log_end_t, (-7, 0), log_end_t)
    else:
        log_lit_t = log_t

    # Log parts. Because fire needs to be in the middle of the logs,
    #   split the logs into two parts: Those appearing behind the fire
    #   and those appearing in front of the fire
    logs_back_t = Image.new("RGBA", (24, 24), self.bgcolor)
    logs_front_t = Image.new("RGBA", (24, 24), self.bgcolor)

    # Back logs
    alpha_over(logs_back_t, log_lit_t, (-1, 7), log_lit_t)
    log_tmp_t = logs_back_t.transpose(Image.FLIP_LEFT_RIGHT)
    alpha_over(logs_back_t, log_tmp_t, (1, -3), log_tmp_t)

    # Front logs
    alpha_over(logs_front_t, log_t, (7, 10), log_t)
    # Due to the awkward drawing order, take a small part of the back
    #   logs that need to be drawn on top of the front logs despite
    #   the front logs being drawn last
    ImageDraw.Draw(log_tmp_t).rectangle((0, 0, 18, 24), outline=(0, 0, 0, 0), fill=(0, 0, 0, 0))
    alpha_over(logs_front_t, log_tmp_t, (1, -3), log_tmp_t)
    log_tmp_t = Image.new("RGBA", (24, 24), self.bgcolor)
    alpha_over(log_tmp_t, log_lit_t, (7, 10), log_lit_t)
    log_tmp_t = log_tmp_t.transpose(Image.FLIP_LEFT_RIGHT)
    alpha_over(logs_front_t, log_tmp_t, (1, -3), log_tmp_t)

    # Compose final image
    img = Image.new("RGBA", (24, 24), self.bgcolor)
    alpha_over(img, bottom_t, (0, 12), bottom_t)
    alpha_over(img, logs_back_t, (0, 0), logs_back_t)
    if data & 0b100:
        fire_t = fire_raw_t.copy()
        if data & 0b11 in [0, 2]:  # North, South
            fire_t = fire_t.transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, fire_t, (4, 4), fire_t)
    alpha_over(img, logs_front_t, (0, 0), logs_front_t)
    if data & 0b11 in [0, 2]:  # North, South
        img = img.transpose(Image.FLIP_LEFT_RIGHT)

    return img


# Bell
@material(blockid=11507, data=list(range(16)), solid=True, transparent=True, nospawn=True)
def bell(self, blockid, data):
    # Do rotation, mask to not clobber attachment data
    data = data & 0b1100 | ((self.rotation + (data & 0b11)) % 4)

    # Load textures
    bell_raw_t = self.load_image("assets/minecraft/textures/entity/bell/bell_body.png")
    bar_raw_t = self.load_image_texture("assets/minecraft/textures/block/dark_oak_planks.png")
    post_raw_t = self.load_image_texture("assets/minecraft/textures/block/stone.png")

    def create_tile(img_src, coord_crop, coord_paste, rot):
        # Takes an image, crops a region, optionally rotates the
        #   texture, then finally pastes it onto a 16x16 image
        img_out = Image.new("RGBA", (16, 16), self.bgcolor)
        img_in = img_src.crop(coord_crop)
        if rot != 0:
            img_in = img_in.rotate(rot, expand=True)
        img_out.paste(img_in, coord_paste)
        return img_out

    # 0 = floor, 1 = ceiling, 2 = single wall, 3 = double wall
    bell_type = (data & 0b1100) >> 2
    # Should the bar/post texture be flipped? Yes if either:
    #   - Attached to floor and East or West facing
    #   - Not attached to floor and North or South facing
    flip_part = ((bell_type == 0 and data & 0b11 in [1, 3]) or
                 (bell_type != 0 and data & 0b11 in [0, 2]))

    # Generate bell
    # Bell side textures varies based on self.rotation
    bell_sides_idx = [(0 - self.rotation) % 4, (3 - self.rotation) % 4]
    # Upper sides
    bell_coord = [x * 6 for x in bell_sides_idx]
    bell_ul_t = create_tile(bell_raw_t, (bell_coord[0], 6, bell_coord[0] + 6, 13), (5, 4), 180)
    bell_ur_t = create_tile(bell_raw_t, (bell_coord[1], 6, bell_coord[1] + 6, 13), (5, 4), 180)
    bell_ul_t = self.transform_image_side(bell_ul_t)
    bell_ur_t = self.transform_image_side(bell_ur_t.transpose(Image.FLIP_LEFT_RIGHT))
    bell_ur_t = bell_ur_t.transpose(Image.FLIP_LEFT_RIGHT)
    # Lower sides
    bell_coord = [x * 8 for x in bell_sides_idx]
    bell_ll_t = create_tile(bell_raw_t, (bell_coord[0], 21, bell_coord[0] + 8, 23), (4, 11), 180)
    bell_lr_t = create_tile(bell_raw_t, (bell_coord[1], 21, bell_coord[1] + 8, 23), (4, 11), 180)
    bell_ll_t = self.transform_image_side(bell_ll_t)
    bell_lr_t = self.transform_image_side(bell_lr_t.transpose(Image.FLIP_LEFT_RIGHT))
    bell_lr_t = bell_lr_t.transpose(Image.FLIP_LEFT_RIGHT)
    # Upper top
    top_rot = (180 + self.rotation * 90) % 360
    bell_ut_t = create_tile(bell_raw_t, (6, 0, 12, 6), (5, 5), top_rot)
    bell_ut_t = self.transform_image_top(bell_ut_t)
    # Lower top
    bell_lt_t = create_tile(bell_raw_t, (8, 13, 16, 21), (4, 4), top_rot)
    bell_lt_t = self.transform_image_top(bell_lt_t)

    bell_t = Image.new("RGBA", (24, 24), self.bgcolor)
    alpha_over(bell_t, bell_lt_t, (0, 8), bell_lt_t)
    alpha_over(bell_t, bell_ll_t, (3, 4), bell_ll_t)
    alpha_over(bell_t, bell_lr_t, (9, 4), bell_lr_t)
    alpha_over(bell_t, bell_ut_t, (0, 3), bell_ut_t)
    alpha_over(bell_t, bell_ul_t, (4, 4), bell_ul_t)
    alpha_over(bell_t, bell_ur_t, (8, 4), bell_ur_t)

    # Generate bar
    if bell_type == 1:  # Ceiling
        # bar_coord:  Left          Right         Top
        bar_coord = [(4, 2, 6, 5), (6, 2, 8, 5), (1, 3, 3, 5)]
        bar_tile_pos = [(7, 1), (7, 1), (7, 7)]
        bar_over_pos = [(6, 3), (7, 2), (0, 0)]
    else:  # Floor, single wall, double wall
        # Note: For a single wall bell, the position of the bar
        #   varies based on facing
        if bell_type == 2 and data & 0b11 in [2, 3]:  # Single wall, North/East facing
            bar_x_sw = 3
            bar_l_pos_sw = (6, 7)
        else:
            bar_x_sw = 0
            bar_l_pos_sw = (4, 8)
        bar_x = [2, None, bar_x_sw, 0][bell_type]
        bar_len = [12, None, 13, 16][bell_type]
        bar_l_pos = [(6, 7), None, bar_l_pos_sw, (4, 8)][bell_type]
        bar_long_coord = (bar_x, 3, bar_x + bar_len, 5)

        bar_coord = [(5, 4, 7, 6), bar_long_coord, bar_long_coord]
        bar_tile_pos = [(2, 1), (bar_x, 1), (bar_x, 7)]
        bar_over_pos = [bar_l_pos, (7, 3), (0, 1)]

    bar_l_t = create_tile(bar_raw_t, bar_coord[0], bar_tile_pos[0], 0)
    bar_r_t = create_tile(bar_raw_t, bar_coord[1], bar_tile_pos[1], 0)
    bar_t_t = create_tile(bar_raw_t, bar_coord[2], bar_tile_pos[2], 0)
    bar_l_t = self.transform_image_side(bar_l_t)
    bar_r_t = self.transform_image_side(bar_r_t.transpose(Image.FLIP_LEFT_RIGHT))
    bar_r_t = bar_r_t.transpose(Image.FLIP_LEFT_RIGHT)
    bar_t_t = self.transform_image_top(bar_t_t)

    bar_t = Image.new("RGBA", (24, 24), self.bgcolor)
    alpha_over(bar_t, bar_t_t, bar_over_pos[2], bar_t_t)
    alpha_over(bar_t, bar_l_t, bar_over_pos[0], bar_l_t)
    alpha_over(bar_t, bar_r_t, bar_over_pos[1], bar_r_t)
    if flip_part:
        bar_t = bar_t.transpose(Image.FLIP_LEFT_RIGHT)

    # Generate post, only applies to floor attached bell
    if bell_type == 0:
        post_l_t = create_tile(post_raw_t, (0, 1, 4, 16), (6,  1), 0)
        post_r_t = create_tile(post_raw_t, (0, 1, 2, 16), (14, 1), 0)
        post_t_t = create_tile(post_raw_t, (0, 0, 2,  4), (14, 6), 0)
        post_l_t = self.transform_image_side(post_l_t)
        post_r_t = self.transform_image_side(post_r_t.transpose(Image.FLIP_LEFT_RIGHT))
        post_r_t = post_r_t.transpose(Image.FLIP_LEFT_RIGHT)
        post_t_t = self.transform_image_top(post_t_t)

        post_back_t = Image.new("RGBA", (24, 24), self.bgcolor)
        post_front_t = Image.new("RGBA", (24, 24), self.bgcolor)
        alpha_over(post_back_t, post_t_t, (0, 1), post_t_t)
        alpha_over(post_back_t, post_l_t, (10, 0), post_l_t)
        alpha_over(post_back_t, post_r_t, (7, 3), post_r_t)
        alpha_over(post_back_t, post_r_t, (6, 3), post_r_t)  # Fix some holes
        alpha_over(post_front_t, post_back_t, (-10, 5), post_back_t)
        if flip_part:
            post_back_t = post_back_t.transpose(Image.FLIP_LEFT_RIGHT)
            post_front_t = post_front_t.transpose(Image.FLIP_LEFT_RIGHT)

    img = Image.new("RGBA", (24, 24), self.bgcolor)
    if bell_type == 0:
        alpha_over(img, post_back_t, (0, 0), post_back_t)
    alpha_over(img, bell_t, (0, 0), bell_t)
    alpha_over(img, bar_t, (0, 0), bar_t)
    if bell_type == 0:
        alpha_over(img, post_front_t, (0, 0), post_front_t)

    return img


# nether roof
# Ancient Debris
solidmodelblock(blockid=[1000], name="ancient_debris")

# Basalt
@material(blockid=[1001, 1002], data=list(range(3)), solid=True)
def basalt(self, blockid, data):
    axis = {0: 'y', 1: 'x', 2: 'z'}[data]
    if blockid == 1001:  # basalt
        return self.build_block_from_model('basalt', {'axis': axis})
    if blockid == 1002:  # polished_basalt
        return self.build_block_from_model('polished_basalt', {'axis': axis})

    
# nether roof
# Blackstone block
solidmodelblock(blockid=[1004], name="blackstone")

# Chain
@material(blockid=11419, data=list(range(3)), solid=True, transparent=True, nospawn=True)
def chain(self, blockid, data):
    tex = self.load_image_texture("assets/minecraft/textures/block/chain.png")
    sidetex = Image.new(tex.mode, tex.size, self.bgcolor)
    mask = tex.crop((0, 0, 6, 16))
    alpha_over(sidetex, mask, (5, 0), mask)

    if data == 0: # y
        return self.build_sprite(sidetex)
    else:
        img = Image.new("RGBA", (24, 24), self.bgcolor)
        sidetex = sidetex.rotate(90)
        side = self.transform_image_side(sidetex)
        otherside = self.transform_image_top(sidetex)

        def draw_x():
            _side = side.transpose(Image.FLIP_LEFT_RIGHT)
            alpha_over(img, _side, (6,3), _side)
            alpha_over(img, otherside, (3,3), otherside)

        def draw_z():
            _otherside = otherside.transpose(Image.FLIP_LEFT_RIGHT)
            alpha_over(img, side, (6,3), side)
            alpha_over(img, _otherside, (0,6), _otherside)

        draw_funcs = [draw_x, draw_z]

        if data == 1: # x
            draw_funcs[self.rotation % len(draw_funcs)]()

        elif data == 2: # z
            draw_funcs[(self.rotation + 1) % len(draw_funcs)]()

        return img

# Respawn anchor
@material(blockid=1037, data=list(range(5)), solid=True)
def respawn_anchor(self, blockid, data):
    return self.build_block_from_model("respawn_anchor_%s" % data)

# nether roof
# soul soil
solidmodelblock(blockid=1020, name="soul_soil")
# nether gold ore
solidmodelblock(blockid=1021, name="nether_gold_ore")

# waxed copper
solidmodelblock(blockid=[1050], name="copper_block")
solidmodelblock(blockid=[1051], name="exposed_copper")
solidmodelblock(blockid=[1052], name="weathered_copper")
solidmodelblock(blockid=[1053], name="oxidized_copper")
# Cut variant
solidmodelblock(blockid=[1058], name="cut_copper")
solidmodelblock(blockid=[1059], name="exposed_cut_copper")
solidmodelblock(blockid=[1060], name="weathered_cut_copper")
solidmodelblock(blockid=[1061], name="oxidized_cut_copper")

# mineral overlay
solidmodelblock(blockid=1063, name="copper_ore")

# deepslate
@material(blockid=1083, data=list(range(3)), solid=True)
def deepslate(self, blockid, data):
    return self.build_block_from_model('deepslate', {'axis': {0: 'y', 1: 'x', 2: 'z'}[data]})


# mineral overlay
solidmodelblock(blockid=1086, name="deepslate_coal_ore")
solidmodelblock(blockid=1087, name="deepslate_iron_ore")
solidmodelblock(blockid=1088, name="deepslate_copper_ore")
solidmodelblock(blockid=1089, name="deepslate_gold_ore")
solidmodelblock(blockid=1090, name="deepslate_emerald_ore")
solidmodelblock(blockid=1091, name="deepslate_lapis_ore")
solidmodelblock(blockid=1092, name="deepslate_diamond_ore")
solidmodelblock(blockid=1093, name="deepslate_redstone_ore")

@material(blockid=1110, data=list(range(16)), transparent=True)
def pointed_dripstone(self, blockid, data):
    up_down = "down" if data & 0b1000 else "up"
    if (data & 4) == 4: # base
        tex = self.load_image_texture("assets/minecraft/textures/block/pointed_dripstone_%s_base.png" % (up_down))
    elif (data & 3) == 3: # frustum
        tex = self.load_image_texture("assets/minecraft/textures/block/pointed_dripstone_%s_frustum.png" % (up_down))
    elif (data & 2) == 2: # middle
        tex = self.load_image_texture("assets/minecraft/textures/block/pointed_dripstone_%s_middle.png" % (up_down))
    elif (data & 1) == 1: # tip_merge
        tex = self.load_image_texture("assets/minecraft/textures/block/pointed_dripstone_%s_tip_merge.png" % (up_down))
    else: # 0 - tip
        tex = self.load_image_texture("assets/minecraft/textures/block/pointed_dripstone_%s_tip.png" % (up_down))
    return self.build_sprite(tex)

@material(blockid=1112, data=0, transparent=True)
def hangings_roots(self, blockid, data):
    tex = self.load_image_texture("assets/minecraft/textures/block/hanging_roots.png")
    return self.build_sprite(tex)


@material(blockid=[1113, 1114, 1115], data=list(range(6)), transparent=True)
def amethyst_bud(self, blockid, data):
    if blockid == 1113:
        tex = self.load_image_texture("assets/minecraft/textures/block/small_amethyst_bud.png")
    elif blockid == 1114:
        tex = self.load_image_texture("assets/minecraft/textures/block/medium_amethyst_bud.png")
    elif blockid == 1115:
        tex = self.load_image_texture("assets/minecraft/textures/block/large_amethyst_bud.png")

    def draw_north():
        rotated = tex.rotate(90)
        side = self.transform_image_side(rotated)
        otherside = self.transform_image_top(rotated)
        otherside = otherside.transpose(Image.FLIP_TOP_BOTTOM)
        alpha_over(img, side, (6, 3), side)
        alpha_over(img, otherside, (0, 6), otherside)

    def draw_south():
        rotated = tex.rotate(-90)
        side = self.transform_image_side(rotated)
        otherside = self.transform_image_top(rotated)
        otherside = otherside.transpose(Image.FLIP_TOP_BOTTOM)
        alpha_over(img, side, (6, 3), side)
        alpha_over(img, otherside, (0, 6), otherside)

    def draw_west():
        rotated = tex.rotate(-90)
        side = self.transform_image_side(rotated)
        side = side.transpose(Image.FLIP_LEFT_RIGHT)
        otherside = self.transform_image_top(rotated)
        otherside = otherside.transpose(Image.FLIP_LEFT_RIGHT)
        otherside = otherside.transpose(Image.FLIP_TOP_BOTTOM)
        alpha_over(img, side, (6, 3), side)
        alpha_over(img, otherside, (0, 6), otherside)

    def draw_east():
        rotated = tex.rotate(90)
        side = self.transform_image_side(rotated)
        side = side.transpose(Image.FLIP_LEFT_RIGHT)
        otherside = self.transform_image_top(rotated)
        otherside = otherside.transpose(Image.FLIP_LEFT_RIGHT)
        otherside = otherside.transpose(Image.FLIP_TOP_BOTTOM)
        alpha_over(img, side, (6, 3), side)
        alpha_over(img, otherside, (0, 6), otherside)

    draw_funcs = [draw_east, draw_south, draw_west, draw_north]

    if data == 0: # down
        tex = tex.transpose(Image.FLIP_TOP_BOTTOM)
        return self.build_sprite(tex)
    elif data == 1: # up
        return self.build_sprite(tex)
    elif data == 5: # north
        img = Image.new("RGBA", (24, 24), self.bgcolor)
        draw_funcs[(self.rotation + 3) % len(draw_funcs)]()
        return img
    elif data == 3: # south
        img = Image.new("RGBA", (24, 24), self.bgcolor)
        draw_funcs[(self.rotation + 1) % len(draw_funcs)]()
        return img
    elif data == 4: # west
        img = Image.new("RGBA", (24,24), self.bgcolor)
        draw_funcs[(self.rotation + 2) % len(draw_funcs)]()
        return img
    elif data == 2: # east
        img = Image.new("RGBA", (24, 24), self.bgcolor)
        draw_funcs[(self.rotation + 0) % len(draw_funcs)]()
        return img

    return self.build_sprite(tex)


@material(blockid=[1116, 1117], data=list(range(2)), transparent=True)
def cave_vines(self, blockid, data):
    if blockid == 1116:
        if data == 1:
            tex = self.load_image_texture("assets/minecraft/textures/block/cave_vines_plant_lit.png")
        else:
            tex = self.load_image_texture("assets/minecraft/textures/block/cave_vines_plant.png")
    elif blockid == 1117:
        if data == 1:
            tex = self.load_image_texture("assets/minecraft/textures/block/cave_vines_lit.png")
        else:
            tex = self.load_image_texture("assets/minecraft/textures/block/cave_vines.png")
    return self.build_sprite(tex)

@material(blockid=1118, data=list(range(6)), transparent=True, solid=True)
def lightning_rod(self, blockid, data):

    # lignting rod default model is for facing 'up'
    # TODO: for generic processing the texture requires uv handling

    # facing = {0: 'down', 1: 'up', 2: 'east', 3: 'south', 4: 'west', 5: 'north'}[data]
    # return self.build_block_from_model('lightning_rod', {'facing': 'north'})

    tex = self.load_image_texture("assets/minecraft/textures/block/lightning_rod.png")
    img = Image.new("RGBA", (24, 24), self.bgcolor)

    mask = tex.crop((0, 4, 2, 16))
    sidetex = Image.new(tex.mode, tex.size, self.bgcolor)
    alpha_over(sidetex, mask, (14, 4), mask)

    mask = tex.crop((0, 0, 4, 4))
    toptex = Image.new(tex.mode, tex.size, self.bgcolor)
    alpha_over(toptex, mask, (12, 0), mask)

    mask = tex.crop((0, 4, 2, 6))
    side_toptex = Image.new(tex.mode, tex.size, self.bgcolor)
    alpha_over(side_toptex, mask, (12, 0), mask)

    def draw_east():
        toptex_rotated = toptex.rotate(90)
        top_side = self.transform_image_side(toptex_rotated)
        top_side = top_side.transpose(Image.FLIP_LEFT_RIGHT)
        top_otherside = self.transform_image_top(toptex)
        top_otherside = top_otherside.transpose(Image.FLIP_LEFT_RIGHT)
        top_top = self.transform_image_side(toptex)

        # top
        alpha_over(img, top_otherside, (6, 6), top_otherside)
        # side
        alpha_over(img, top_side, (8, 7), top_side)
        alpha_over(img, top_top, (6, 2), top_top)

        roated_side = sidetex.rotate(90)
        side = self.transform_image_side(roated_side)
        side = side.transpose(Image.FLIP_TOP_BOTTOM)
        otherside = self.transform_image_top(sidetex)
        otherside = otherside.transpose(Image.FLIP_TOP_BOTTOM)
        side_top = self.transform_image_side(side_toptex)

        alpha_over(img, otherside, (-7, 4), otherside)
        alpha_over(img, side, (5, -1), side)
        alpha_over(img, side_top, (-2, 9), side_top)

    def draw_south():
        roated_side = sidetex.rotate(90)
        side = self.transform_image_side(roated_side)
        otherside = self.transform_image_top(sidetex)

        alpha_over(img, side, (3, 6), side)
        alpha_over(img, otherside, (-8, 6), otherside)

        toptex_rotated = toptex.rotate(90)
        top_side = self.transform_image_side(toptex_rotated)
        top_otherside = self.transform_image_top(toptex)
        top_top = self.transform_image_side(toptex)
        top_top = top_top.transpose(Image.FLIP_LEFT_RIGHT)

        alpha_over(img, top_side, (15, 12), top_side)
        alpha_over(img, top_otherside, (5, 10), top_otherside)
        alpha_over(img, top_top, (17, 7), top_top)

    def draw_west():
        roated_side = sidetex.rotate(90)
        side = self.transform_image_side(roated_side)
        side = side.transpose(Image.FLIP_LEFT_RIGHT)
        otherside = self.transform_image_top(sidetex)
        otherside = otherside.transpose(Image.FLIP_LEFT_RIGHT)

        alpha_over(img, side, (10, 6), side)
        alpha_over(img, otherside, (8, 6), otherside)

        toptex_rotated = toptex.rotate(90)
        top_side = self.transform_image_side(toptex_rotated)
        top_side = top_side.transpose(Image.FLIP_LEFT_RIGHT)
        top_otherside = self.transform_image_top(toptex)
        top_otherside = top_otherside.transpose(Image.FLIP_LEFT_RIGHT)
        top_top = self.transform_image_side(toptex)

        # top
        alpha_over(img, top_otherside, (-3, 10), top_otherside)
        # side
        alpha_over(img, top_side, (0, 11), top_side)
        alpha_over(img, top_top, (-3, 7), top_top)

    def draw_north():
        roated_side = sidetex.rotate(90)
        side = self.transform_image_side(roated_side)
        otherside = self.transform_image_top(sidetex)

        alpha_over(img, side, (4, 7), side)
        alpha_over(img, otherside, (-6, 7), otherside)

        toptex_rotated = toptex.rotate(90)
        top_side = self.transform_image_side(toptex_rotated)
        top_otherside = self.transform_image_top(toptex)
        top_top = self.transform_image_side(toptex)
        top_top = top_top.transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, top_otherside, (-4, 6), top_otherside)
        alpha_over(img, top_side, (5, 7), top_side)
        alpha_over(img, top_top, (8, 3), top_top)

    draw_funcs = [draw_east, draw_south, draw_west, draw_north]

    if data == 1: # up
        side = self.transform_image_side(sidetex)
        otherside = side.transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, side, (0, 6 - 4), side)
        alpha_over(img, otherside, (12, 6 - 4), otherside)

        top_top = self.transform_image_top(toptex)
        top_side = self.transform_image_side(toptex)
        top_otherside = top_side.transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, top_side, (0, 6 - 4), top_side)
        alpha_over(img, top_otherside, (12, 6 - 4), top_otherside)
        alpha_over(img, top_top, (0, 5), top_top)
    elif data == 0: # down
        toptex_flipped = toptex.transpose(Image.FLIP_TOP_BOTTOM)
        top_top = self.transform_image_top(toptex)
        top_side = self.transform_image_side(toptex_flipped)
        top_otherside = top_side.transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, top_side, (0, 6 - 4), top_side)
        alpha_over(img, top_otherside, (12, 6 - 4), top_otherside)
        alpha_over(img, top_top, (0, 14), top_top)

        flipped = sidetex.transpose(Image.FLIP_TOP_BOTTOM)
        side_top = self.transform_image_top(side_toptex)
        side = self.transform_image_side(flipped)
        otherside = side.transpose(Image.FLIP_LEFT_RIGHT)
        alpha_over(img, side, (0, 6 - 4), side)
        alpha_over(img, otherside, (12, 6 - 4), otherside)
        alpha_over(img, side_top, (2, 6), side_top)
    elif data == 3: # south
        draw_funcs[(self.rotation + 1) % len(draw_funcs)]()
    elif data == 4: # west
        draw_funcs[(self.rotation + 2) % len(draw_funcs)]()
    elif data == 2: # east
        draw_funcs[(self.rotation + 0) % len(draw_funcs)]()
    elif data == 5: # north
        draw_funcs[(self.rotation + 3) % len(draw_funcs)]()

    return img


@material(blockid=1119, data=list(range(1 << 6)), transparent=True)
def glow_lichen(self, blockid, data):
    tex = self.load_image_texture("assets/minecraft/textures/block/glow_lichen.png")

    bottom = tex if data & 1 << 0 else None
    top = tex if data & 1 << 1 else None
    east = tex if data & 1 << 2 else None
    south = tex if data & 1 << 3 else None
    west = tex if data & 1 << 4 else None
    north = tex if data & 1 << 5 else None

    if self.rotation == 0:
        return self.build_full_block(top, north, east, west, south, bottom)
    elif self.rotation == 1:
        return self.build_full_block(top, west, north, south, east, bottom)
    elif self.rotation == 2:
        return self.build_full_block(top, south, west, east, north, bottom)
    else: # self.rotation == 3:
        return self.build_full_block(top, east, south, north, west, bottom)


@material(blockid=1120, data=list(range(1)), transparent=True)
def spore_blossom(self, blockid, data):
    leaf = self.load_image_texture("assets/minecraft/textures/block/spore_blossom.png")
    base = self.load_image_texture("assets/minecraft/textures/block/spore_blossom_base.png")
    img = Image.new("RGBA", (24, 24), self.bgcolor)

    side_leaf = self.transform_image_top(leaf)
    alpha_over(img, side_leaf, (-6, -5), side_leaf)

    roated_leaf = leaf.rotate(90)
    side_leaf = self.transform_image_top(roated_leaf)
    alpha_over(img, side_leaf, (-7, 4), side_leaf)

    roated_leaf = roated_leaf.rotate(90)
    side_leaf = self.transform_image_top(roated_leaf)
    alpha_over(img, side_leaf, (5, 4), side_leaf)

    roated_leaf = roated_leaf.rotate(90)
    side_leaf = self.transform_image_top(roated_leaf)
    alpha_over(img, side_leaf, (5, -5), side_leaf)

    base_top = self.transform_image_top(base)
    alpha_over(img, base_top, (0, 0), base_top)
    return img

# Render all blocks not explicitly declared before
# Must run last to prevent being hidden by blocks with fixed IDs
unbound_models()
