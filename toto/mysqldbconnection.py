from tornado.database import *
from toto.exceptions import *
from toto.session import *
from time import time, mktime
from datetime import datetime
import toto.secret as secret
import base64
import uuid
import hmac
import hashlib
import random
import string
import cPickle as pickle

class MySQLdbSession(TotoSession):
  _account = None

  class MySQLdbAccount(TotoAccount):
    
    def __init__(self, session):
      super(MySQLdbAccount, self).__init__(session)
      self.properties['account_id'] = session.account_id

    def _load_property(self, *args):
      return self._session._db.get('select ' + ', '.join(args) + ' from account where account_id = %s', self._session.account_id)

    def _save_property(self, *args):
      self._session._db.execute('update account set ' + ', '.join(['%s = %%s' % k for k in args]) + ' where account_id = %s', [self[k] for k in args] + [self._session.account_id,])

    def __setitem__(self, key, value):
      if key != 'account_id':
        super(MySQLdbAccount, self).__setitem__(key, value)
    
  def __init__(self, db, session_data):
    super(MySQLdbSession, self).__init__(db, session_data)
    self.account_id = session_data['account_id']

  def get_account(self):
    if not self._account:
      self._account = MySQLdbSession.MySQLdbAccount(self)
    return self._account
  
  def refresh(self):
    session_data = self.db.get("select session.session_id, session.expires, session.state, account.user_id, account.account_id from session join account on account.account_id = session.account_id where session.session_id = %s", session_id)
    self.__init__(session_data)

  def save(self):
    if not self._verified:
      raise TotoException(ERROR_NOT_AUTHORIZED, "Not authorized")
    self._db.execute("update session set state = %s where session_id = %s", pickle.dumps(self.state), self.session_id)

class MySQLdbConnection():

  def create_tables(self):
    if not self.db.get('''show tables like "account"'''):
      self.db.execute('''CREATE TABLE IF NOT EXISTS `account` (
        `account_id` int(8) unsigned NOT NULL AUTO_INCREMENT,
        `password` char(48) DEFAULT NULL,
        `user_id` varchar(45) NOT NULL,
        PRIMARY KEY (`account_id`),
        UNIQUE KEY `user_id_UNIQUE` (`user_id`),
        INDEX `user_id_password` (`user_id`, `password`)
      )''')
    if not self.db.get('''show tables like "session"'''):
      self.db.execute('''CREATE TABLE IF NOT EXISTS `session` (
        `session_id` char(32) NOT NULL,
        `account_id` int(8) unsigned NOT NULL,
        `expires` double NOT NULL,
        `state` blob,
        PRIMARY KEY (`session_id`),
        INDEX (`expires`),
        FOREIGN KEY (`account_id`) REFERENCES `account`(`account_id`)
      )''')

  def __init__(self, host, database, username, password, default_session_ttl=24*60*60*365, anon_session_ttl=24*60*60, session_renew=0, anon_session_renew=0):
    self.db = Connection(host, database, username, password)
    self.create_tables()
    self.default_session_ttl = default_session_ttl
    self.anon_session_ttl = anon_session_ttl or self.default_session_ttl
    self.session_renew = session_renew or self.default_session_ttl
    self.anon_session_renew = anon_session_renew or self.anon_session_ttl

  def create_account(self, user_id, password, additional_values={}, **values):
    user_id = user_id.lower()
    if self.db.get("select account_id from account where user_id = %s", user_id):
      raise TotoException(ERROR_USER_ID_EXISTS, "User ID already in use.")
    values.update(additional_values)
    values['user_id'] = user_id
    values['password'] = secret.password_hash(password)
    self.db.execute("insert into account (" + ', '.join([k for k in values]) + ") values (" + ','.join(['%s' for k in values]) + ")", *[values[k] for k in values])

  def create_session(self, user_id=None, password=None):
    if not user_id:
      user_id = ''
    user_id = user_id.lower()
    account = user_id and self.db.get("select * from account where user_id = %s", user_id)
    if user_id and not account or not secret.verify_password(password, account['password']):
      raise TotoException(ERROR_USER_NOT_FOUND, "Invalid user ID or password")
    session_id = base64.b64encode(uuid.uuid4().bytes, '-_')[:-2]
    self.db.execute("delete from session where account_id = %s and expires <= %s", account['account_id'], time())
    expires = time() + (user_id and self.session_ttl or self.anon_session_ttl)
    self.db.execute("insert into session (account_id, expires, session_id) values (%s, %s, %s)", account['account_id'], expires, session_id)
    session = MySQLdbSession(self.db, {'user_id': user_id, 'expires': expires, 'session_id': session_id})
    session._verified = True
    return session

  def retrieve_session(self, session_id, hmac_data, data):
    session_data = self.db.get("select session.session_id, session.expires, session.state, account.user_id from session join account on account.account_id = session.account_id where session.session_id = %s and session.expires > %s", session_id, time())
    if not session_data:
      return None
    user_id = session_data['user_id']
    if session_data['expires'] < (time() + (user_id and self.session_renew or self.anon_session_renew)):
      session_data['expires'] = time() + (user_id and self.session_ttl or self.anon_session_ttl)
      self.db.execute("update session set expires = %s where session_id = %s", session['expires'], session_id)
    session = MySQLdbSession(self.db, session_data)
    if data and hmac_data != base64.b64encode(hmac.new(str(user_id), data, hashlib.sha1).digest()):
      raise TotoException(ERROR_INVALID_HMAC, "Invalid HMAC")
    session._verified = True
    return session

  def remove_session(self, session_id):
    self.db.execute("delete from session where session_id = %s", session_id)

  def clear_sessions(self, user_id):
    user_id = user_id.lower()
    self.db.execute("delete from session using session join account on account.account_id = session.account_id where account.user_id = %s", user_id)

  def change_password(self, user_id, password, new_password):
    user_id = user_id.lower()
    account = self.db.get("select account_id, user_id, password from account where user_id = %s", user_id)
    if not account or not secret.verify_password(password, account['password']):
      raise TotoException(ERROR_USER_NOT_FOUND, "Invalid user ID or password")
    self.db.execute("update account set password = %s where account_id = %s", secret.password_hash(new_password), account['account_id'])
    self.clear_sessions(user_id)

  def generate_password(self, user_id):
    user_id = user_id.lower()
    account = self.db.get("select account_id, user_id from account where user_id = %s", user_id)
    if not account:
      raise TotoException(ERROR_USER_NOT_FOUND, "Invalid user ID")
    pass_chars = string.ascii_letters + string.digits
    new_password = ''.join([random.choice(pass_chars) for x in xrange(10)])
    self.db.execute("update account set password = %s where account_id = %s", secret.password_hash(new_password), account['account_id'])
    self.clear_sessions(user_id)
    return new_password
