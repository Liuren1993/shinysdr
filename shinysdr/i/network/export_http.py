# Copyright 2013, 2014, 2015, 2016 Kevin Reid <kpreid@switchb.org>
# 
# This file is part of ShinySDR.
# 
# ShinySDR is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# ShinySDR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with ShinySDR.  If not, see <http://www.gnu.org/licenses/>.

"""Exports ExportedState/Cell object interfaces over HTTP."""

from __future__ import absolute_import, division

import json
import os.path
import urllib
import weakref

from twisted.internet.protocol import ProcessProtocol
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET
from twisted.web import template

from shinysdr.i.network.base import prepath_escaped, renderElement, serialize, template_path
from shinysdr.values import IWritableCollection


class _CellResource(Resource):
    isLeaf = True

    def __init__(self, cell, wcommon):
        self._cell = cell
        self.__note_dirty = wcommon.note_dirty

    def grparse(self, value):
        raise NotImplementedError()

    def grrender(self, value, request):
        return str(value)

    def render_GET(self, request):
        return self.grrender(self._cell.get(), request)

    def render_PUT(self, request):
        data = request.content.read()
        self._cell.set(self.grparse(data))
        request.setResponseCode(204)
        self.__note_dirty()
        return ''
    
    def resourceDescription(self):
        return self._cell.description()


class ValueCellResource(_CellResource):
    def __init__(self, cell, wcommon):
        _CellResource.__init__(self, cell, wcommon)

    def grparse(self, value):
        return json.loads(value)

    def grrender(self, value, request):
        return serialize(value).encode('utf-8')


class BlockResource(Resource):
    isLeaf = False

    def __init__(self, block, wcommon, deleteSelf):
        Resource.__init__(self)
        self._block = block
        self.__wcommon = wcommon
        self._deleteSelf = deleteSelf
        self._dynamic = block.state_is_dynamic()
        # Weak dict ensures that we don't hold references to blocks that are no longer held by this block
        self._blockResourceCache = weakref.WeakKeyDictionary()
        if not self._dynamic:  # currently dynamic blocks can only have block children
            self._blockCells = {}
            for key, cell in block.state().iteritems():
                if cell.isBlock():
                    self._blockCells[key] = cell
                else:
                    self.putChild(key, ValueCellResource(cell, self.__wcommon))
        self.__element = _BlockHtmlElement(wcommon)
    
    def getChild(self, name, request):
        if self._dynamic:
            curstate = self._block.state()
            if name in curstate:
                cell = curstate[name]
                if cell.isBlock():
                    return self.__getBlockChild(name, cell.get())
        else:
            if name in self._blockCells:
                return self.__getBlockChild(name, self._blockCells[name].get())
        # old-style-class super call
        return Resource.getChild(self, name, request)
    
    def __getBlockChild(self, name, block):
        r = self._blockResourceCache.get(block)
        if r is None:
            r = self.__makeChildBlockResource(name, block)
            self._blockResourceCache[block] = r
        return r
    
    def __makeChildBlockResource(self, name, block):
        def deleter():
            if not IWritableCollection.providedBy(self._block):
                raise Exception('Block is not a writable collection')
            self._block.delete_child(name)
        return BlockResource(block, self.__wcommon, deleter)
    
    def render_GET(self, request):
        accept = request.getHeader('Accept')
        if accept is not None and 'application/json' in accept:  # TODO: Implement or obtain correct Accept interpretation
            request.setHeader('Content-Type', 'application/json')
            return serialize(self.resourceDescription()).encode('utf-8')
        else:
            request.setHeader('Content-Type', 'text/html;charset=utf-8')
            return renderElement(request, self.__element)
    
    def render_POST(self, request):
        """currently only meaningful to create children of CollectionResources"""
        block = self._block
        if not IWritableCollection.providedBy(block):
            raise Exception('Block is not a writable collection')
        assert request.getHeader('Content-Type') == 'application/json'
        reqjson = json.load(request.content)
        key = block.create_child(reqjson)  # note may fail
        self.__wcommon.note_dirty()
        url = request.prePathURL() + '/receivers/' + urllib.quote(key, safe='')
        request.setResponseCode(201)  # Created
        request.setHeader('Location', url)
        # TODO consider a more useful response
        return serialize(url).encode('utf-8')
    
    def render_DELETE(self, request):
        self._deleteSelf()
        self.__wcommon.note_dirty()
        request.setResponseCode(204)  # No Content
        return ''
    
    def resourceDescription(self):
        return self._block.state_description()
    
    def isForBlock(self, block):
        return self._block is block


class _BlockHtmlElement(template.Element):
    """
    Template element for HTML page for an arbitrary block.
    """
    loader = template.XMLFile(os.path.join(template_path, 'block.template.xhtml'))
    
    def __init__(self, wcommon):
        self.__wcommon = wcommon
    
    @template.renderer
    def title(self, request, tag):
        return tag(request.prepath)
    
    @template.renderer
    def quoted_state_url(self, request, tag):
        return tag(serialize(self.__wcommon.make_websocket_url(request,
            prepath_escaped(request))))


class FlowgraphVizResource(Resource):
    """A resource which is an image of the given flow graph's dot_graph() visualization."""
    isLeaf = True
    
    def __init__(self, reactor, block):
        self.__reactor = reactor
        self.__block = block
    
    def render_GET(self, request):
        request.setHeader('Content-Type', 'image/png')
        process = self.__reactor.spawnProcess(
            _DotProcessProtocol(request),
            '/usr/bin/env',
            env=None,  # inherit environment
            args=['env', 'dot', '-Tpng'],
            childFDs={
                0: 'w',
                1: 'r',
                2: 2
            })
        process.pipes[0].write(self.__block.dot_graph())
        process.pipes[0].loseConnection()
        return NOT_DONE_YET


class _DotProcessProtocol(ProcessProtocol):
    def __init__(self, request):
        self.__request = request
    
    def outReceived(self, data):
        self.__request.write(data)
    
    def outConnectionLost(self):
        self.__request.finish()
