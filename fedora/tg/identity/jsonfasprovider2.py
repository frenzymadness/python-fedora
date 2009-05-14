# -*- coding: utf-8 -*-
#
# Copyright © 2007-2008  Red Hat, Inc. All rights reserved.
#
# This copyrighted material is made available to anyone wishing to use, modify,
# copy, or redistribute it subject to the terms and conditions of the GNU
# General Public License v.2.  This program is distributed in the hope that it
# will be useful, but WITHOUT ANY WARRANTY expressed or implied, including the
# implied warranties of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.  You should have
# received a copy of the GNU General Public License along with this program;
# if not, write to the Free Software Foundation, Inc., 51 Franklin Street,
# Fifth Floor, Boston, MA 02110-1301, USA. Any Red Hat trademarks that are
# incorporated in the source code or documentation are not subject to the GNU
# General Public License and may only be used or replicated with the express
# permission of Red Hat, Inc.
#
# Adapted from code in the TurboGears project licensed under the MIT license.

'''
This plugin provides authentication by integrating with the Fedora Account
System using JSON calls.


.. moduleauthor:: Toshio Kuratomi <tkuratom@redhat.com>
.. moduleauthor:: Ricky Zhou <ricky@fedoraproject.org>
'''

import crypt
try:
    from hashlib import sha1 as hash_constructor
except ImportError:
    from sha import new as hash_constructor

from turbogears import config, identity
from turbogears.identity import set_login_attempted
import cherrypy

from fedora.client import BaseClient, AccountSystem, FedoraServiceError
from fedora import _, __version__

import logging
log = logging.getLogger('turbogears.identity.jsonfasprovider')

try:
    # pylint: disable-msg=W0104
    # :W0104: we need to figure out if we have a native set type
    set, frozenset
except NameError:
    # :W0622: We need a set type on earlier pythons.
    from sets import Set as set # pylint: disable-msg=W0622
    from sets import ImmutableSet as frozenset # pylint: disable-msg=W0622

if config.get('identity.ssl', False):
    fas_user = config.get('fas.username', None)
    fas_password = config.get('fas.password', None)
    if not (fas_user and fas_password):
        raise identity.IdentityConfigurationException(
                _('Cannot enable ssl certificate auth via identity.ssl without'
                ' setting fas.usernamme and fas.password for authorization'))
    __url = config.get('fas.url', None)
    if __url:
        fas = AccountSystem(__url, username=config.get('fas.username'),
                password=config.get('fas.password'))


class JsonFasIdentity(BaseClient):
    '''Associate an identity with a person in the auth system.
    '''
    cookie_name = config.get('visit.cookie.name', 'tg-visit')
    fas_url = config.get('fas.url', 'https://admin.fedoraproject.org/accounts/')
    useragent = 'JsonFasIdentity/%s' % __version__
    cache_session = False

    def __init__(self, visit_key=None, user=None, username=None, password=None,
            using_ssl=False):
        # The reason we have both _retrieved_user and _user is this:
        # _user is set if both the user is authenticated and a csrf_token is
        # present.
        # _retrieved_user actually caches the user info from the server.
        # Sometimes we have to determine if a user is only lacking a token,
        # then retrieved_user comes in handy.
        self._retrieved_user = None
        self.log = log
        self.visit_key = visit_key
        session_id = visit_key
        self._group_ids = frozenset()
        self.using_ssl = using_ssl
        if user:
            self._user = user
            self._user_retrieved = user
            self._groups = frozenset(
                    [g['name'] for g in user['approved_memberships']]
                    )

        debug = config.get('jsonfas.debug', False)
        super(JsonFasIdentity, self).__init__(self.fas_url,
                useragent=self.useragent, debug=debug,
                username=username, password=password,
                session_id=session_id, cache_session=self.cache_session)

        if self.debug:
            import inspect
            caller = inspect.getouterframes(inspect.currentframe())[1][3]
            self.log.debug('JsonFasIdentity.__init__ caller: %s' % caller)

        cherrypy.response.simple_cookie[self.cookie_name] = visit_key

        self.login(using_ssl)
        self.log.debug('Leaving JsonFasIdentity.__init__')

    def send_request(self, method, req_params=None, auth=False):
        '''Make an HTTP Request to a server method.

        We need to override the send_request provided by ``BaseClient`` to
        keep the visit_key in sync.
        '''
        self.log.debug('entering jsonfas send_request')
        if self.session_id != self.visit_key:
            # When the visit_key changes (because the old key had expired or
            # been deleted from the db) change the visit_key in our variables
            # and the session cookie to be sent back to the client.
            self.visit_key = self.session_id
            cherrypy.response.simple_cookie[self.cookie_name] = self.visit_key
        self.log.debug('leaving jsonfas send_request')
        return super(JsonFasIdentity, self).send_request(method, req_params,
                auth)

    def __retrieve_user(self):
        '''Attempt to load the user from the visit_key.

        :returns: a user or None
        '''
        if self.debug:
            import inspect
            caller = inspect.getouterframes(inspect.currentframe())[2][3]
            self.log.debug('JSONFASPROVIDER.send_request caller: %s' % caller)

        if self._retrieved_user:
            return self._retrieved_user
        # I hope this is a safe place to double-check the SSL variables.
        # TODO: Double check my logic with this - is it unnecessary to
        # check that the username matches up?
        if self.using_ssl:
            if cherrypy.request.headers['X-Client-Verify'] != 'SUCCESS':
                self.logout()
                return None
            # Retrieve the user information differently when using ssl
            try:
                person = fas.person_by_username(self.username, auth=True)
            except Exception, e: # pylint: disable-msg=W0703
                # :W0703: Any errors have to result in no user being set.  The
                # rest of the framework doesn't know what to do otherwise.
                self.log.warning(_('jsonfasprovider, ssl, returned errors'
                    ' from send_request: %s') % e)
                person = None
            self._retrieved_user = person or None
            return self._retrieved_user
        # pylint: disable-msg=W0702
        try:
            data = self.send_request('user/view', auth=True)
        except Exception, e: # pylint: disable-msg=W0703
            # :W0703: Any errors have to result in no user being set.  The rest
            # of the framework doesn't know what to do otherwise.
            self.log.warning(_('jsonfasprovider returned errors from'
                ' send_request: %s') % e)
            return None
        # pylint: enable-msg=W0702

        self._retrieved_user = data['person'] or None
        return self._retrieved_user

    def _get_user(self):
        '''Get user instance for this identity.'''
        visit = self.visit_key
        if not visit:
            # No visit, no user
            self._user = None
        else:
            if not (self.username and self.password):
                # Unless we were given the user_name and password to login on
                # this request, a CSRF token is required
                if (not '_csrf_token' in cherrypy.request.params or
                        cherrypy.request.params['_csrf_token'] !=
                        hash_constructor(self.visit_key).hexdigest()):
                    self.log.info("Bad _csrf_token")
                    if '_csrf_token' in cherrypy.request.params:
                        self.log.info("visit: %s token: %s" % (self.visit_key,
                            cherrypy.request.params['_csrf_token']))
                    else:
                        self.log.info('No _csrf_token present')
                    cherrypy.request.fas_identity_failure_reason = 'bad_csrf'
                    self._user = None

        # pylint: disable-msg=W0704
            try:
                return self._user
            except AttributeError:
                # User hasn't already been set
                # Attempt to load the user. After this code executes, there
                # *will* be a _user attribute, even if the value is None.
                self._user = self.__retrieve_user()
            self._groups = frozenset(
                    [g['name'] for g in self._user.approved_memberships]
                    )
        # pylint: enable-msg=W0704
        return self._user
    user = property(_get_user)

    def _get_token(self):
        '''Get the csrf token for this identity'''
        if self.visit_key:
            return hash_constructor(self.visit_key).hexdigest()
        else:
            return ''
    csrf_token = property(_get_token)

    def _get_user_name(self):
        '''Get user name of this identity.'''
        if self.debug:
            import inspect
            caller = inspect.getouterframes(inspect.currentframe())[1][3]
            self.log.debug('JsonFasProvider._get_user_name caller: %s' % caller)

        if not self.user:
            return None
        return self.user.username
    user_name = property(_get_user_name)

    ### TG: Same as TG-1.0.8
    def _get_user_id(self):
        '''
        Get user id of this identity.
        '''
        if not self.user:
            return None
        return self.user.user_id
    user_id = property(_get_user_id)

    ### TG: Same as TG-1.0.8
    def _get_anonymous(self):
        '''
        Return True if not logged in.
    '''
        return not self.user
    anonymous = property(_get_anonymous)

    def _get_only_token(self):
        '''
        In one specific instance in the login template we need to know whether
        an anonymous user is just lacking a token.
        '''
        if self.__retrieve_user():
            # user is valid, just the token is missing
            return True

        # Else the user still has to login
        return False
    only_token = property(_get_only_token)

    def _get_permissions(self):
        '''Get set of permission names of this identity.'''
        # pylint: disable-msg=R0201
        # :R0201: method is part of the TG Identity API
        ### TG difference: No permissions in FAS
        return frozenset()
    permissions = property(_get_permissions)

    def _get_display_name(self):
        '''Return the user's display name.

        .. warning::
            This is not a TG standard attribute.  Don't use this if you want
            to be compatible with other identity providers.
        '''
        if not self.user:
            return None
        return self.user['human_name']
    display_name = property(_get_display_name)

    def _get_groups(self):
        '''Return the groups that a user is a member of.'''
        try:
            return self._groups
        except AttributeError: # pylint: disable-msg=W0704
            # :W0704: Groups haven't been computed yet
            pass
        if not self.user:
            # User and groups haven't been returned.  Since the json call
            # computes both user and groups, this will now be set.
            self._groups = frozenset()
        return self._groups
    groups = property(_get_groups)

    def _get_group_ids(self):
        '''Get set of group IDs of this identity.'''
        try:
            return self._group_ids
        except AttributeError: # pylint: disable-msg=W0704
            # :W0704: Groups haven't been computed yet
            pass
        if not self.groups:
            self._group_ids = frozenset()
        else:
            self._group_ids = frozenset([g.id for g in
                self._user.approved_memberships])
        return self._group_ids
    group_ids = property(_get_group_ids)

    ### TG: Same as TG-1.0.8
    def _get_login_url(self):
        '''Get the URL for the login page.'''
        return identity.get_failure_url()
    login_url = property(_get_login_url)

    def login(self, using_ssl=False):
        '''Send a request so that we associate the visit_cookie with the user

        :kwarg using_ssl: Boolean that tells whether ssl was used to authenticate
        '''
        if not using_ssl:
            # This is only of value if we have username and password
            # which we don't if using ssl certificates
            self.send_request('', auth=True)
        self.using_ssl = using_ssl

    def logout(self):
        '''
        Remove the link between this identity and the visit.
        '''
        if not self.visit_key:
            return
        # Call Account System Server logout method
        self.send_request('logout', auth=True)

class JsonFasIdentityProvider(object):
    '''
    IdentityProvider that authenticates users against the fedora account system
    '''
    def __init__(self):
        # Default encryption algorithm is to use plain text passwords
        algorithm = config.get('identity.saprovider.encryption_algorithm', None)
        self.log = log
        # pylint: disable-msg=W0212
        # TG does this so we shouldn't get rid of it.
        self.encrypt_password = lambda pw: \
                                    identity._encrypt_password(algorithm, pw)

    def create_provider_model(self):
        '''
        Create the database tables if they don't already exist.
        '''
        # No database tables to create because the db is behind the FAS2
        # server
        pass

    def validate_identity(self, user_name, password, visit_key):
        '''
        Look up the identity represented by user_name and determine whether the
        password is correct.

        Must return either None if the credentials weren't valid or an object
        with the following properties:

            :user_name: original user name
            :user: a provider dependant object (TG_User or similar)
            :groups: a set of group IDs
            :permissions: a set of permission IDs

        :arg user_name: user_name we're authenticating.  If None, we'll try
            to lookup a username from SSL variables
        :arg password: password to authenticate user_name with
        :arg visit_key: visit_key from the user's session
        '''
        using_ssl = False
        if not user_name and config.get('identity.ssl'):
            if cherrypy.request.headers['X-Client-Verify'] == 'SUCCESS':
                user_name = cherrypy.request.headers['X-Client-CN']
                cherrypy.request.fas_provided_username = user_name
                using_ssl = True

        # pylint: disable-msg=R0201
        # TG identity providers have this method so we can't get rid of it.
        try:
            user = JsonFasIdentity(visit_key, username=user_name,
                    password=password, using_ssl=using_ssl)
        except FedoraServiceError, e:
            self.log.warning(_('Error logging in %(user)s: %(error)s') % {
                'user': user_name, 'error': e})
            return None

        return user

    def validate_password(self, user, user_name, password):
        '''
        Check the supplied user_name and password against existing credentials.
        Note: user_name is not used here, but is required by external
        password validation schemes that might override this method.
        If you use SqlAlchemyIdentityProvider, but want to check the passwords
        against an external source (i.e. PAM, LDAP, Windows domain, etc),
        subclass SqlAlchemyIdentityProvider, and override this method.

        :arg user: User information.
        :arg user_name: Given username.  Not used.
        :arg password: Given, plaintext password.
        :returns: True if the password matches the username.  Otherwise False.
            Can return False for problems within the Account System as well.
        '''
        # pylint: disable-msg=R0201,W0613
        # :R0201: TG identity providers must instantiate this method.

        # crypt.crypt(stuff, '') == ''
        # Just kill any possibility of blanks.
        if not user.password:
            return False
        if not password:
            return False

        # pylint: disable-msg=W0613
        # :W0613: TG identity providers have this method
        return user.password == crypt.crypt(password, user.password)

    def load_identity(self, visit_key):
        '''Lookup the principal represented by visit_key.

        :arg visit_key: The session key for whom we're looking up an identity.
        :return: an object with the following properties:
            :user_name: original user name
            :user: a provider dependant object (TG_User or similar)
            :groups: a set of group IDs
            :permissions: a set of permission IDs
        '''
        # pylint: disable-msg=R0201
        # :R0201: TG identity providers must instantiate this method.
        ident = JsonFasIdentity(visit_key)
        if 'csrf_login' in cherrypy.request.params:
            cherrypy.request.params.pop('csrf_login')
            set_login_attempted(True)
        return ident

    def anonymous_identity(self):
        '''Returns an anonymous user object

        :return: an object with the following properties:
            :user_name: original user name
            :user: a provider dependant object (TG_User or similar)
            :groups: a set of group IDs
            :permissions: a set of permission IDs
        '''
        # pylint: disable-msg=R0201
        # :R0201: TG identity providers must instantiate this method.
        return JsonFasIdentity(None)

    def authenticated_identity(self, user):
        '''
        Constructs Identity object for user that has no associated visit_key.

        :arg user: The user structure the identity is constructed from
        :return: an object with the following properties:
            :user_name: original user name
            :user: a provider dependant object (TG_User or similar)
            :groups: a set of group IDs
            :permissions: a set of permission IDs
        '''
        # pylint: disable-msg=R0201
        # :R0201: TG identity providers must instantiate this method.
        return JsonFasIdentity(None, user)