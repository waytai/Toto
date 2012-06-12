import os
import zmq
from zmq.devices.basedevice import ProcessDevice
import tornado
from tornado.options import define, options
import logging
import zlib
import cPickle as pickle
import sys
import time
from multiprocessing import Process, cpu_count

define("database", metavar='mysql|mongodb|none', default="mongodb", help="the database driver to use")
define("mysql_host", default="localhost:3306", help="MySQL database 'host:port'")
define("mysql_database", type=str, help="Main MySQL schema name")
define("mysql_user", type=str, help="Main MySQL user")
define("mysql_password", type=str, help="Main MySQL user password")
define("mongodb_host", default="localhost", help="MongoDB host")
define("mongodb_port", default=27017, help="MongoDB port")
define("mongodb_database", default="toto_server", help="MongoDB database")
define("daemon", metavar='start|stop|restart', help="Start, stop or restart this script as a daemon process. Use this setting in conf files, the shorter start, stop, restart aliases as command line arguments. Requires the multiprocessing module.")
define("processes", default=-1, help="The number of daemon processes to run, pass 0 to run only the load balancer. Negative numbers will run one worker per cpu")
define("pidfile", default="toto.worker.pid", help="The path to the pidfile for daemon processes will be named <path>.<num>.pid (toto.worker.pid -> toto.worker.0.pid)")
define("method_module", default='methods', help="The root module to use for method lookup")
define("remote_event_receivers", type=str, help="A comma separated list of remote event address that this event manager should connect to. e.g.: 'tcp://192.168.1.2:8889'", multiple=True)
define("event_init_module", default=None, type=str, help="If defined, this module's 'invoke' function will be called with the EventManager instance after the main event handler is registered (e.g.: myevents.setup)")
define("start", default=False, help="Alias for daemon=start for command line usage - overrides daemon setting.")
define("stop", default=False, help="Alias for daemon=start for command line usage - overrides daemon setting.")
define("restart", default=False, help="Alias for daemon=start for command line usage - overrides daemon setting.")
define("nodaemon", default=False, help="Alias for daemon='' for command line usage - overrides daemon setting.")
define("startup_function", default=None, type=str, help="An optional function to run on startup - e.g. module.function. The function will be called for each server instance before the server start listening as function(connection=<active database connection>, application=<tornado.web.Application>).")
define("debug", default=False, help="Set this to true to prevent Toto from nicely formatting generic errors. With debug=True, errors will print to the command line")
define("event_port", default=8999, help="The address to listen to event connections on - due to message queuing, servers use the next higher port as well")
define("worker_address", default="tcp://*:55555", help="The service will bind to this address with a zmq PULL socket and listen for incoming tasks. Tasks will be load balanced to all workers. If this is set to an empty string, workers will connect directly to worker_socket_address.")
define("worker_socket_address", default="ipc:///tmp/workerservice.sock", help="The load balancer will use this address to coordinate tasks between local workers")

#convert p to the absolute path, insert ".i" before the last "." or at the end of the path
def pid_path_with_id(p, i):
  (d, f) = os.path.split(os.path.abspath(p))
  components = f.rsplit('.', 1)
  f = '%s.%s' % (components[0], i)
  if len(components) > 1:
    f += "." + components[1]
  return os.path.join(d, f)

class TotoWorkerService():

  def __load_options(self, conf_file=None, **kwargs):
    for k in kwargs:
      options[k].set(kwargs[k])
    if conf_file:
      tornado.options.parse_config_file(conf_file)
    tornado.options.parse_command_line()
    if options.start:
      options['daemon'].set('start')
    elif options.stop:
      options['daemon'].set('stop')
    elif options.restart:
      options['daemon'].set('restart')
    elif options.nodaemon:
      options['daemon'].set('')

  def __init__(self, conf_file=None, **kwargs):
    module_options = {'method_module', 'event_init_module'}
    function_options = {'startup_function'}
    original_argv, sys.argv = sys.argv, [i for i in sys.argv if i.strip('-').split('=')[0] in module_options]
    self.__load_options(conf_file, **{i: kwargs[i] for i in kwargs if i in module_options})
    modules = {getattr(options, i) for i in module_options if getattr(options, i)}
    for module in modules:
      __import__(module)
    function_modules = {getattr(options, i).rsplit('.', 1)[0] for i in function_options if getattr(options, i)}
    for module in function_modules:
      __import__(module)
    sys.argv = original_argv
    #clear root logger handlers to prevent duplicate logging if user has specified a log file
    if options.log_file_prefix:
      root_logger = logging.getLogger()
      for handler in [h for h in root_logger.handlers]:
        root_logger.removeHandler(handler)
    self.__load_options(conf_file, **kwargs)
    #clear method_module references so we can fully reload with new options
    for module in modules:
      for i in (m for m in sys.modules.keys() if m.startswith(module)):
        del sys.modules[i]
    for module in function_modules:
      for i in (m for m in sys.modules.keys() if m.startswith(module)):
        del sys.modules[i]
    #prevent the reloaded module from re-defining options
    define, tornado.options.define = tornado.options.define, lambda *args, **kwargs: None
    self.__event_init = options.event_init_module and __import__(options.event_init_module) or None
    self.__method_module = options.method_module and __import__(options.method_module) or None
    tornado.options.define = define

  def __run_server(self):
    balancer = None
    if options.worker_address:
      balancer = ProcessDevice(zmq.STREAMER, zmq.PULL, zmq.PUSH)
      balancer.bind_in(options.worker_address)
      balancer.bind_out(options.worker_socket_address)
      balancer.setsockopt_in(zmq.IDENTITY, 'PULL')
      balancer.setsockopt_out(zmq.IDENTITY, 'PUSH')
      balancer.start()

    def start_server_process(module):
      db_connection = None
      if options.database == "mongodb":
        from mongodbconnection import MongoDBConnection
        db_connection = MongoDBConnection(options.mongodb_host, options.mongodb_port, options.mongodb_database)
      elif options.database == "mysql":
        from mysqldbconnection import MySQLdbConnection
        db_connection = MySQLdbConnection(options.mysql_host, options.mysql_database, options.mysql_user, options.mysql_password)

      if options.remote_event_receivers:
        from toto.events import EventManager
        event_manager = EventManager.instance()
        if options.remote_instances:
          for address in options.remote_event_receivers.split(','):
            event_manager.register_server(address)
        init_module = self.__event_init
        if init_module:
          init_module.invoke(event_manager)
    
      worker = TotoWorker(module, options.worker_socket_address, db_connection)
      if options.startup_function:
        startup_path = options.startup_function.rsplit('.')
        __import__(startup_path[0]).__dict__[startup_path[1]](worker=worker, db_connection=db_connection)
      worker.start()
    count = options.processes if options.processes >= 0 else cpu_count()
    processes = []
    for i in xrange(count):
      proc = Process(target=start_server_process, args=(self.__method_module,))
      proc.daemon = True
      processes.append(proc)
      proc.start()
    if count == 0:
      print 'Starting load balancer. Listening on "%s". Routing to "%s"' % (options.worker_address, options.worker_socket_address)
    else:
      print "Starting %s worker process%s. %s." % (count, count > 1 and 'es' or '', options.worker_address and ('Listening on "%s"' % options.worker_address) or ('Connecting to "%s"' % options.worker_socket_address))
    if options.daemon:
      i = 1
      for proc in processes:
        with open(pid_path_with_id(options.pidfile, i), 'w') as f:
          f.write(str(proc.pid))
        i += 1
      if balancer:
        with open(pid_path_with_id(options.pidfile, i), 'w') as f:
          f.write(str(balancer.launcher.pid))
    for proc in processes:
      proc.join()
    if balancer:
      balancer.join()

  def run(self): 
    if options.daemon:
      import multiprocessing
      import signal, re

      pattern = pid_path_with_id(options.pidfile, r'\d+').replace('.', r'\.')
      piddir = os.path.dirname(pattern)
      existing_pidfiles = [pidfile for pidfile in (os.path.join(piddir, fn) for fn in os.listdir(os.path.dirname(pattern))) if re.match(pattern, pidfile)]

      if options.daemon == 'stop' or options.daemon == 'restart':
        for pidfile in existing_pidfiles:
          with open(pidfile, 'r') as f:
            pid = int(f.read())
            try:
              os.kill(pid, signal.SIGTERM)
            except OSError as e:
              if e.errno != 3:
                raise
            print "Stopped server %s" % pid 
          os.remove(pidfile)

      if options.daemon == 'start' or options.daemon == 'restart':
        import sys
        if existing_pidfiles:
          print "Not starting, pidfile%s exist%s at %s" % (len(existing_pidfiles) > 1 and 's' or '', len(existing_pidfiles) == 1 and 's' or '', ', '.join(existing_pidfiles))
          return
        pidfile = pid_path_with_id(options.pidfile, 0)
        #fork and only continue on child process
        if not os.fork():
          #detach from controlling terminal
          os.setsid()
          #fork again and write pid to pidfile from parent, run server on child
          pid = os.fork()
          if pid:
            with open(pidfile, 'w') as f:
              f.write(str(pid))
          else:
            self.__run_server()

      if options.daemon not in ('start', 'stop', 'restart'):
        print "Invalid daemon option: " + options.daemon

    else:
      self.__run_server()

class TotoWorker():
  def __init__(self, method_module, socket_address, db_connection):
    self.context = zmq.Context()
    self.socket = self.context.socket(zmq.PULL)
    self.socket_address = socket_address
    self.method_module = method_module
    self.db_connection = db_connection
    self.db = db_connection.db
    if options.debug:
      from traceback import format_exc
      def log_error(self, e):
        logging.error(format_exc())
      TotoWorker.log_error = log_error
  
  def log_error(self, e):
    logging.error(repr(e))

  def start(self):
    self.socket.connect(self.socket_address)
    while True:
      try:
        message = pickle.loads(zlib.decompress(self.socket.recv()))
        logging.info(message['method'])
        method = self.method_module
        for i in message['method'].split('.'):
          method = getattr(method, i)
        method.invoke(self, message['parameters'])
      except Exception as e:
        self.log_error(e)