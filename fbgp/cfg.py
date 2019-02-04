from oslo_config import cfg
from oslo_config import types

opts = [
        cfg.StrOpt('server_addr', default='127.0.0.1', help='Server IP address.'),
        cfg.Opt('server_port', type=types.Integer(1024, 65535), default=9090,
            help='Server port number.'),
        ]

CONF = cfg.ConfigOpts()
CONF.register_cli_opts(opts)



