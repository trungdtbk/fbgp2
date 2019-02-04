import logging
import sys

def get_logger(log_name, log_file=None, level='info'):
    """return a logger object"""
    logger = logging.getLogger(name=log_name)
    formatter = logging.Formatter('%(asctime)s %(name)s %(levelname)s %(message)s')
    if log_file:
        file_hdlr = logging.FileHandler(log_file)
        file_hdlr.setFormatter(formatter)
        logger.addHandler(file_hdlr)
    else:
        st_hdlr = logging.StreamHandler(sys.stderr)
        st_hdlr.setFormatter(formatter)
        logger.addHandler(st_hdlr)
    logger.setLevel(level.upper())
    return logger

def get_system_id():
    """return an ID (use the eth0's mac address) to identify this instance with the route server."""
    try:
        mac = open('/sys/class/net/eth0/address').readline()
    except:
        mac = '00:00:00:00:00:00'
    return mac.strip().replace(':', '')



