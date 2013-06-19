# -*- coding: utf-8 -*-
##
##
## This file is part of Indico.
## Copyright (C) 2002 - 2013 European Organization for Nuclear Research (CERN).
##
## Indico is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 3 of the
## License, or (at your option) any later version.
##
## Indico is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
## General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Indico;if not, see <http://www.gnu.org/licenses/>.
from flask import request, session
from urlparse import urljoin
from indico.web.flask.util import ResponseUtil


import copy
import time
import os
import sys
import random
import StringIO
from datetime import datetime, timedelta

from ZODB.POSException import ConflictError, POSKeyError
from ZEO.Exceptions import ClientDisconnected
import oauth2 as oauth

from MaKaC.common import fossilize
from MaKaC.webinterface.pages.conferences import WPConferenceModificationClosed

import MaKaC.webinterface.urlHandlers as urlHandlers
import MaKaC.webinterface.pages.errors as errors

from MaKaC.accessControl import AccessWrapper
from MaKaC.common import DBMgr, Config, security
from MaKaC.errors import MaKaCError, ModificationError, AccessError, KeyAccessError, TimingError, ParentTimingError, EntryTimingError, FormValuesError, NoReportError, NotFoundError, HtmlScriptError, HtmlForbiddenTag, ConferenceClosedError, HostnameResolveError, BadRefererError
from indico.modules.oauth.errors import OAuthError
from MaKaC.webinterface.mail import GenericMailer
from xml.sax.saxutils import escape

from MaKaC.common.utils import truncate
from MaKaC.common.logger import Logger
from MaKaC.common.contextManager import ContextManager
from indico.util.i18n import _, availableLocales

from MaKaC.plugins import PluginsHolder
from MaKaC.plugins.base import OldObservable
from MaKaC.plugins.RoomBooking.common import rb_check_user_access

from indico.util.redis import RedisError


class RequestHandlerBase(OldObservable):

    _uh = None

    def __init__(self, req=None):
        if req is not None:
            raise Exception("Received request object")

    def _checkProtection( self ):
        """
        """
        pass

    def _getAuth(self):
        """
        Returns True if current user is a user or has either a modification key in their session.
        """
        return bool(session.get('modifKeys')) or bool(self._getUser())

    def getAW( self ):
        """
        Returns the access wrapper related to this session/user
        """
        return self._aw

    def _getUser(self):
        """
        Returns the current user
        """
        return self._aw.getUser()

    def _setUser(self, newUser=None):
        """
        Sets the current user
        """
        self._aw.setUser(newUser)

    def getRequestURL(self, secure=False):
        """
        Reconstructs the request URL
        """
        if secure:
            return urljoin(Config.getInstance().getBaseSecureURL(), request.path)
        else:
            return request.url

    def use_https(self):
        """
        If the RH must be HTTPS and there is a BaseSecurURL, then use it!
        """
        return self._tohttps and Config.getInstance().getBaseSecureURL()

    def getRequestParams( self ):
        return self._params

    def getRequestHTTPHeaders( self ):
        return request.headers

    def _setLang(self, params=None):

        # allow to choose the lang from params
        if params and 'lang' in params:
            newLang = params.get('lang', '')
            for lang in availableLocales:
                if newLang.lower() == lang.lower():
                    session.lang = lang
                    break

        lang = session.lang
        Logger.get('i18n').debug("lang:%s" % lang)
        if lang is None:
            lang = "en_GB"
        from indico.util.i18n import setLocale
        setLocale(lang)

    def getHostIP(self):
        return request.remote_addr

    def _getTruncatedParams(self):
        """Truncates params"""
        params = {}
        for key, value in self._reqParams.iteritems():
            if key == 'password':
                params[key] = '[password hidden, len=%d]' % len(value)
            elif isinstance(value, basestring):
                params[key] = truncate(value, 1024)
            else:
                params[key] = value
        return params

    accessWrapper = property(getAW)


class RH(RequestHandlerBase):
    """This class is the base for request handlers of the application. A request
        handler will be instantiated when a web request arrives to mod_python;
        the mp layer will forward the request to the corresponding request
        handler which will know which action has to be performed (displaying a
        web page or performing some operation and redirecting to another page).
        Request handlers will be responsible for parsing the parameters coming
        from a mod_python request, handle the errors which occurred during the
        action to perform, managing the sessions, checking security for each
        operation (thus they implement the access control system of the web
        interface).
        It is important to encapsulate all this here as in case of changing
        the web application framework we'll just need to adapt this layer (the
        rest of the system wouldn't need any change).

        Attributes:
            _uh - (URLHandler) Associated URLHandler which points to the
                current rh.
            _req - UNUSED/OBSOLETE, always None
            _requestStarted - (bool) Flag which tells whether a DB transaction
                has been started or not.
            _aw - (AccessWrapper) Current access information for the rh.
            _target - (Locable) Reference to an object which is the destination
                of the operations needed to carry out the rh. If set it must
                provide (through the standard Locable interface) the methods
                to get the url parameters in order to reproduce the access to
                the rh.
            _reqParams - (dict) Dictionary containing the received HTTP
                 parameters (independently of the method) transformed into
                 python data types. The key is the parameter name while the
                 value should be the received paramter value (or values).
    """
    _tohttps = False # set this value to True for the RH that must be HTTPS when there is a BaseSecureURL
    _doNotSanitizeFields = []
    _isMobile = True # this value means that the generated web page can be mobile

    def __init__( self, req ):
        """Constructor. Initialises the rh setting up basic attributes so it is
            able to process the request.

            Parameters:
                req - (mod_python.Request) mod_python request received for the
                    current rh.
        """
        RequestHandlerBase.__init__(self, req)
        self._responseUtil = ResponseUtil()
        self._requestStarted = False
        self._aw = AccessWrapper()  #Fill in the aw instance with the current information
        self._target = None
        self._reqParams = {}
        self._startTime = None
        self._endTime = None
        self._tempFilesToDelete = []
        self._redisPipeline = None
        self._doProcess = True  #Flag which indicates whether the RH process
                                #   must be carried out; this is useful for
                                #   the checkProtection methods when they
                                #   detect that an inmediate redirection is
                                #   needed

    # Methods =============================================================

    def getTarget( self ):
        return self._target

    def isMobile(self):
        return self._isMobile

    def _setSessionUser( self ):
        """
        """
        self._aw.setUser(session.user)

    @property
    def csrf_token(self):
        return session.csrf_token

    def _getRequestParams( self ):
        return self._reqParams

    def getRequestParams( self ):
        return self._getRequestParams()

    def _disableCaching(self):
        """
        Disables caching, i.e. for materials
        """

        # IE doesn't seem to like 'no-cache' Cache-Control headers...
        if request.user_agent.browser == 'msie':
            # actually, the only way to safely disable caching seems to be this one
            self._responseUtil.headers["Cache-Control"] = "private"
            self._responseUtil.headers["Expires"] = "-1"
        else:
            self._responseUtil.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            self._responseUtil.headers["Pragma"] = "no-cache"

    def _redirect(self, targetURL, status=303):
        targetURL = str(targetURL)
        if "\r" in targetURL or "\n" in targetURL:
            raise MaKaCError(_("http header CRLF injection detected"))
        self._responseUtil.redirect = (targetURL, status)

    def _changeRH(self, rh, params):
        """Calls the specified RH after processing this one"""
        self._responseUtil.call = lambda: rh(None).process(params)

    def _checkHttpsRedirect(self):
        """
        If HTTPS must be used but it is not, redirect!
        """
        if self.use_https() and not request.is_secure:
            self._redirect(self.getRequestURL(secure=True))
            return True
        else:
            return False

    def _normaliseListParam( self, param ):
        if not isinstance(param, list):
                return [ param ]
        return param

    def _processError( self, ex ):
        """
        """
        raise

    def _checkParams( self, params ):
        """
        """
        pass

    def _process( self ):
        """
        """
        pass

    def _checkCSRF(self):
        # Check referer for POST requests. We do it here so we can properly use indico's error handling
        if Config.getInstance().getCSRFLevel() < 3 or request.method != 'POST':
            return
        referer = request.referrer
        # allow empty - otherwise we might lock out paranoid users blocking referers
        if not referer:
            return
        # valid http referer
        if referer.startswith(Config.getInstance().getBaseURL()):
            return
        # valid https referer - if https is enabled
        base_secure = Config.getInstance().getBaseSecureURL()
        if base_secure and referer.startswith(base_secure):
            return
        raise BadRefererError('This operation is not allowed from an external referer.')

    def _processGeneralError(self,e):
        """Treats general errors occured during the process of a RH.
        """

        Logger.get('requestHandler').info('Request %s finished with: "%s"' % (request, e))

        p=errors.WPGenericError(self)
        return p.display()

    def _processUnexpectedError(self,e):
        """Unexpected errors
        """

        Logger.get('requestHandler').exception('Request %s failed: "%s"' % (request, e))
        p=errors.WPUnexpectedError(self)
        return p.display()

    def _processHostnameResolveError(self,e):
        """Unexpected errors
        """

        Logger.get('requestHandler').exception('Request %s failed: "%s"' % (request, e))
        p=errors.WPHostnameResolveError(self)
        return p.display()


    def _processAccessError(self,e):
        """Treats access errors occured during the process of a RH.
        """
        Logger.get('requestHandler').info('Request %s finished with AccessError: "%s"' % (request, e))

        self._responseUtil.status = 403
        p=errors.WPAccessError(self)
        return p.display()

    def _processKeyAccessError(self,e):
        """Treats access errors occured during the process of a RH.
        """
        Logger.get('requestHandler').info('Request %s finished with KeyAccessError: "%s"' % (request, e))

        # We are going to redirect to the page asking for access key
        # and so it must be https if there is a BaseSecureURL. And that's
        # why we set _tohttps to True.
        self._tohttps = True
        if self._checkHttpsRedirect():
            return
        p=errors.WPKeyAccessError(self)
        return p.display()

    def _processModificationError(self,e):
        """Treats modification errors occured during the process of a RH.
        """

        Logger.get('requestHandler').info('Request %s finished with ModificationError: "%s"' % (request, e))

        p=errors.WPModificationError(self)
        return p.display()

    def _processConferenceClosedError(self,e):
        """Treats access to modification pages for conferences when they are closed.
        """
        p = WPConferenceModificationClosed( self, e._conf )
        return p.display()

    def _processTimingError(self,e):
        """Treats timing errors occured during the process of a RH.
        """

        Logger.get('requestHandler').info('Request %s finished with TimingError: "%s"' % (request, e))

        p=errors.WPTimingError(self,e)
        return p.display()

    def _processNoReportError(self,e):
        """Process errors without reporting
        """

        Logger.get('requestHandler').info('Request %s finished with NoReportError: "%s"' % (request, e))

        p=errors.WPNoReportError(self,e)
        return p.display()

    def _processNotFoundError(self,e):
        """Process not found error; uses NoReportError template
        """

        Logger.get('requestHandler').info('Request %s finished with NotFoundError: "%s"' % (request, e))

        self._responseUtil.status = 404
        p=errors.WPNoReportError(self,e)
        return p.display()

    def _processParentTimingError(self,e):
        """Treats timing errors occured during the process of a RH.
        """

        Logger.get('requestHandler').info('Request %s finished with ParentTimingError: "%s"' % (request, e))

        p=errors.WPParentTimingError(self,e)
        return p.display()

    def _processEntryTimingError(self,e):
        """Treats timing errors occured during the process of a RH.
        """

        Logger.get('requestHandler').info('Request %s finished with EntryTimingError: "%s"' % (request, e))

        p=errors.WPEntryTimingError(self,e)
        return p.display()

    def _processFormValuesError(self,e):
        """Treats user input related errors occured during the process of a RH.
        """

        Logger.get('requestHandler').info('Request %s finished with FormValuesError: "%s"' % (request, e))

        p=errors.WPFormValuesError(self,e)
        return p.display()

    def _processHtmlScriptError(self, e):

        Logger.get('requestHandler').info('Request %s finished with ProcessHtmlScriptError: "%s"' % (request, e))

        p=errors.WPHtmlScriptError(self, escape(str(e)))
        return p.display()

    def _processRestrictedHTML(self, e):

        Logger.get('requestHandler').info('Request %s finished with ProcessRestrictedHTMLError: "%s"' % (request, e))

        p=errors.WPRestrictedHTML(self, escape(str(e)))
        return p.display()

    def process(self, params):
        profile = Config.getInstance().getProfile()
        proffilename = ""
        res = ""
        MAX_RETRIES = 10
        retry = MAX_RETRIES
        textLog = []
        self._startTime = datetime.now()

        # clear the context
        ContextManager.destroy()
        ContextManager.set('currentRH', self)

        #redirect to https if necessary
        if self._checkHttpsRedirect():
            return self._responseUtil.make_redirect()


        DBMgr.getInstance().startRequest()
        self._startRequestSpecific2RH()     # I.e. implemented by Room Booking request handlers
        textLog.append("%s : Database request started" % (datetime.now() - self._startTime))
        Logger.get('requestHandler').info('[pid=%s] Request %s started (%s)' % (
            os.getpid(), request, request.path))

        # notify components that the request has started
        self._notify('requestStarted')

        forcedConflicts = Config.getInstance().getForceConflicts()
        try:
            while retry>0:

                if retry < MAX_RETRIES:
                    # notify components that the request is being retried
                    self._notify('requestRetry', MAX_RETRIES - retry)

                try:
                    Logger.get('requestHandler').info('\t[pid=%s] from host %s' % (os.getpid(), self.getHostIP()))
                    try:
                        # clear the fossile cache at the start of each request
                        fossilize.clearCache()
                        # delete all queued emails
                        GenericMailer.flushQueue(False)
                        # clear the existing redis pipeline
                        if self._redisPipeline:
                            self._redisPipeline.reset()

                        DBMgr.getInstance().sync()
                        # keep a link to the web session in the access wrapper
                        # this is used for checking access/modification key existence
                        # in the user session
                        self._aw.setIP(self.getHostIP())
                        self._setSessionUser()
                        self._setLang(params)
                        if self._getAuth():
                            if self._getUser():
                                Logger.get('requestHandler').info('Request %s identified with user %s (%s)' % (
                                    request, self._getUser().getFullName(), self._getUser().getId()))
                            if not self._tohttps and Config.getInstance().getAuthenticatedEnforceSecure():
                                self._tohttps = True
                                if self._checkHttpsRedirect():
                                    return self._responseUtil.make_redirect()

                        self._checkCSRF()
                        #if self._getUser() != None and self._getUser().getId() == "893":
                        #    profile = True
                        self._reqParams = copy.copy( params )
                        self._checkParams( self._reqParams )

                        self._checkProtection()
                        security.Sanitization.sanitizationCheck(self._target,
                                               self._reqParams,
                                               self._aw, self._doNotSanitizeFields)
                        if self._doProcess:
                            if profile:
                                import profile, pstats
                                proffilename = os.path.join(Config.getInstance().getTempDir(), "stone%s.prof" % str(random.random()))
                                result = [None]
                                profile.runctx("result[0] = self._process()", globals(), locals(), proffilename)
                                res = result[0]
                            else:
                                res = self._process()

                        # notify components that the request has finished
                        self._notify('requestFinished')
                        # Raise a conflict error if enabled. This allows detecting conflict-related issues easily.
                        if retry > (MAX_RETRIES - forcedConflicts):
                            raise ConflictError
                        self._endRequestSpecific2RH( True ) # I.e. implemented by Room Booking request handlers
                        DBMgr.getInstance().endRequest( True )

                        Logger.get('requestHandler').info('Request %s successful' % request)
                        #request succesfull, now, doing tas that must be done only once
                        try:
                            GenericMailer.flushQueue(True) # send emails
                            self._deleteTempFiles()
                        except:
                            Logger.get('mail').exception('Mail sending operation failed')
                            pass
                        # execute redis pipeline if we have one
                        if self._redisPipeline:
                            try:
                                self._redisPipeline.execute()
                            except RedisError:
                                Logger.get('redis').exception('Could not execute pipeline')
                        break
                    except MaKaCError, e:
                        #DBMgr.getInstance().endRequest(False)
                        res = self._processError(e)
                except (ConflictError, POSKeyError):
                    import traceback
                    # only log conflict if it wasn't forced
                    if retry <= (MAX_RETRIES - forcedConflicts):
                        Logger.get('requestHandler').warning('Conflict in Database! (Request %s)\n%s' % (request, traceback.format_exc()))
                    self._abortSpecific2RH()
                    DBMgr.getInstance().abort()
                    retry -= 1
                    continue
                except ClientDisconnected:
                    Logger.get('requestHandler').warning('Client Disconnected! (Request %s)' % request)
                    self._abortSpecific2RH()
                    DBMgr.getInstance().abort()
                    retry -= 1
                    time.sleep(10-retry)
                    continue
        except KeyAccessError, e:
            #Key Access error treatment
            res = self._processKeyAccessError( e )
            self._endRequestSpecific2RH( False )
            DBMgr.getInstance().endRequest(False)
        except AccessError, e:
            #Access error treatment
            res = self._processAccessError( e )
            self._endRequestSpecific2RH( False )
            DBMgr.getInstance().endRequest(False)
        except HostnameResolveError, e:
            res = self._processHostnameResolveError( e )
            self._endRequestSpecific2RH( False )
            DBMgr.getInstance().endRequest(False)
        except ModificationError, e:
            #Modification error treatment
            res = self._processModificationError( e )
            self._endRequestSpecific2RH( False )
            DBMgr.getInstance().endRequest(False)
        except ParentTimingError, e:
            #Modification error treatment
            res = self._processParentTimingError( e )
            self._endRequestSpecific2RH( False )
            DBMgr.getInstance().endRequest(False)
        except EntryTimingError, e:
            #Modification error treatment
            res = self._processEntryTimingError( e )
            self._endRequestSpecific2RH( False )
            DBMgr.getInstance().endRequest(False)
        except TimingError, e:
            #Modification error treatment
            res = self._processTimingError( e )
            self._endRequestSpecific2RH( False )
            DBMgr.getInstance().endRequest(False)
        except FormValuesError, e:
            #Error filling the values of a form
            res = self._processFormValuesError( e )
            self._endRequestSpecific2RH( False )
            DBMgr.getInstance().endRequest(False)
        except ConferenceClosedError, e:
            #Modification error treatment
            res = self._processConferenceClosedError( e )
            self._endRequestSpecific2RH( False )
            DBMgr.getInstance().endRequest(False)
        except NoReportError, e:
            #Error without report option
            res = self._processNoReportError( e )
            DBMgr.getInstance().endRequest(False)
        except NotFoundError, e:
            #File not fond error
            res = self._processNotFoundError( e )
            DBMgr.getInstance().endRequest(False)
        except HtmlScriptError,e:
            res = self._processHtmlScriptError(e)
            DBMgr.getInstance().endRequest(False)
        except HtmlForbiddenTag,e:
            res = self._processRestrictedHTML(e)
            DBMgr.getInstance().endRequest(False)
        except MaKaCError, e:
            res = self._processGeneralError( e )
            DBMgr.getInstance().endRequest(False)
            if Config.getInstance().getPropagateAllExceptions():
                raise
        except ValueError, e:
            res = self._processGeneralError( e )
            DBMgr.getInstance().endRequest(False)
            if Config.getInstance().getPropagateAllExceptions():
                raise
        except OAuthError, e:
            from indico.util import json
            res = json.dumps(e.fossilize())
            header = oauth.build_authenticate_header(realm=Config.getInstance().getBaseSecureURL())
            self._responseUtil.headers.extend(header)
            self._responseUtil.content_type = 'application/json'
            self._responseUtil.status = e.code
            DBMgr.getInstance().endRequest(False)
        except Exception, e:
            res = self._processUnexpectedError(e)
            self._endRequestSpecific2RH(False)
            DBMgr.getInstance().endRequest(False)

            if Config.getInstance().getEmbeddedWebserver() or Config.getInstance().getPropagateAllExceptions():
                # Re-raise to get the nice werkzeug exception view
                raise

            #cancels any redirection
            self._responseUtil.redirect = None
            self._responseUtil.status = 500

        totalTime = (datetime.now() - self._startTime)
        textLog.append("%s : Request ended"%totalTime)

        # log request timing
        if profile and totalTime > timedelta(0, 1) and os.path.isfile(proffilename):
            rep = Config.getInstance().getTempDir()
            stats = pstats.Stats(proffilename)
            stats.strip_dirs()
            stats.sort_stats('cumulative', 'time', 'calls')
            stats.dump_stats(os.path.join(rep, "IndicoRequestProfile.log"))
            output = StringIO.StringIO()
            sys.stdout = output
            stats.print_stats(100)
            sys.stdout = sys.__stdout__
            s = output.getvalue()
            f = file(os.path.join(rep, "IndicoRequest.log"), 'a+')
            f.write("--------------------------------\n")
            f.write("URL     : " + request.url + "\n")
            f.write("%s : start request\n"%self._startTime)
            f.write("params:%s"%params)
            f.write("\n".join(textLog))
            f.write("\n")
            f.write("retried : %d\n"%(10-retry))
            f.write(s)
            f.write("--------------------------------\n\n")
            f.close()
        if profile and proffilename != "" and os.path.exists(proffilename):
            os.remove(proffilename)

        if self._responseUtil.call:
            return self._responseUtil.make_call()

        # In case of no process needed, we should return empty string to avoid erroneous ouput
        # specially with getVars breaking the JS files.
        if not self._doProcess or res is None:
            return self._responseUtil.make_empty()

        return self._responseUtil.make_response(res)

    def _deleteTempFiles( self ):
        if len(self._tempFilesToDelete) > 0:
            for f in self._tempFilesToDelete:
                if f is not None:
                    os.remove(f)

    def _startRequestSpecific2RH( self ):
        """
        Works like DBMgr.getInstance().startRequest() but is specific to
        request handler. It is used to connect to other database only
        in choosen request handlers.

        I.e. all Room Booking request handlers override this
        method to connect to Room Booking backend.
        """
        pass

    def _endRequestSpecific2RH( self, commit ):
        """
        Works like DBMgr.getInstance().endRequest() but is specific to
        request handler. It is used to disconnect from other database only
        in choosen request handlers.

        I.e. all Room Booking request handlers override this
        method to disconnect from Room Booking backend.
        """
        pass

    def _syncSpecific2RH( self ):
        """
        Works like DBMgr.getInstance().sync() but is specific to
        request handler. It is used to connect to other database only
        in choosen request handlers.

        I.e. all Room Booking request handlers override this
        method to sync backend.
        """
        pass

    def _abortSpecific2RH( self ):
        """
        Works like DBMgr.getInstance().abort() but is specific to
        request handler. It is used to abort transactions of other database
        only in choosen request handlers.

        I.e. all Room Booking request handlers override this method.
        """
        pass

    # Properties =============================================================

    relativeURL = None


from MaKaC.rb_location import CrossLocationDB
import MaKaC.common.info as info

class RoomBookingDBMixin:     # It's _not_ RH
    """
    Goal:
    Only _some_ Request Handlers should connect to
    room booking database.

    Mix in this class into all Request Handlers,
    which must use Room Booking backend.

    Usage:

    class RHExample( RoomBookingDBMixin, RHProtected ):
        pass

    NOTE: it is important to put RoomBookingDBMixin as first
    base class.
    """

    def _startRequestSpecific2RH( self ):
        minfo = info.HelperMaKaCInfo.getMaKaCInfoInstance()
        if minfo.getRoomBookingModuleActive():
            CrossLocationDB.connect()

    def _endRequestSpecific2RH( self, commit = True ):
        minfo = info.HelperMaKaCInfo.getMaKaCInfoInstance()
        if minfo.getRoomBookingModuleActive():
            if commit: CrossLocationDB.commit()
            else: CrossLocationDB.rollback()
            CrossLocationDB.disconnect()

    def _syncSpecific2RH( self ):
        minfo = info.HelperMaKaCInfo.getMaKaCInfoInstance()
        if minfo.getRoomBookingModuleActive():
            CrossLocationDB.sync()

    def _abortSpecific2RH( self ):
        minfo = info.HelperMaKaCInfo.getMaKaCInfoInstance()
        if minfo.getRoomBookingModuleActive():
            CrossLocationDB.rollback()


class RHProtected(RH):

    def _getLoginURL( self ):
        return urlHandlers.UHSignIn.getURL(self.getRequestURL())

    def _checkSessionUser( self ):
        """
        """

        if self._getUser() == None:
            self._redirect( self._getLoginURL() )
            self._doProcess = False

    def _checkProtection( self ):
        self._checkSessionUser()


class RHRoomBookingProtected(RHProtected):

    def _checkSessionUser( self ):
        user = self._getUser()
        if user == None:
            self._redirect( self._getLoginURL() )
            self._doProcess = False
        else:
            try:
                if PluginsHolder().getPluginType("RoomBooking").isActive():
                    if not rb_check_user_access(user):
                        raise AccessError()
            except KeyError:
                pass


class RHDisplayBaseProtected(RHProtected):

    def _checkProtection( self ):
        if not self._target.canAccess( self.getAW() ):
            from MaKaC.conference import Link, LocalFile, Category
            if isinstance(self._target,Link) or isinstance(self._target,LocalFile):
                target = self._target.getOwner()
            else:
                target = self._target
            if not isinstance(self._target, Category) and target.isProtected():
                if target.getAccessKey() != "" or target.getConference() and target.getConference().getAccessKey() != "":
                    raise KeyAccessError()
            if self._getUser() == None:
                self._checkSessionUser()
            else:
                raise AccessError()


class RHModificationBaseProtected(RHProtected):

    _allowClosed = False

    def _checkProtection( self ):
        if not self._target.canModify( self.getAW() ):
            if self._target.getModifKey() != "":
                raise ModificationError()
            if self._getUser() == None:
                self._checkSessionUser()
            else:
                raise ModificationError()
        if hasattr(self._target, "getConference") and not self._allowClosed:
            if self._target.getConference().isClosed():
                raise ConferenceClosedError(self._target.getConference())

