import re
import ipaddress

class Policy:
    """A policy has a filter expression and a list of actions
    Take a route object (prefix, as_path,...), check if it match the filter,
    then apply actions and return the modified route object
    """

    def __init__(self, filter_, actions=None):
        self._filter = filter_
        self._actions = actions

    def evaluate(self, route):
        if self._filter.match(route):
            self._apply_actions(route)
            return route
        return None

    def _apply_actions(self, route):
        """Not doing anything at the moment
        """
        return route

    @classmethod
    def default(cls):
        return cls(filter_=FilterANY())

    def __str__(self):
        return "Policy(filter=%s, actions=%s)" % (self._filter, self._actions)
    __repr__ = __str__


class Filter:
    @classmethod
    def parse(cls, filter_str):
        """A filter has a RPSL-like format. For example { 1.0.0.0/20^+ }."""
        filter_str = filter_str.strip().lower()
        match = re.search(r'or|and', filter_str)
        if match:
            first_filter_str = filter_str[:match.start()].strip()
            filter_str = filter_str[match.start() + len(match.group()):].strip()
            filter1 = Filter.parse(first_filter_str)
            filter2 = Filter.parse(filter_str)
            if match.group() == 'and':
                return FilterAND(filter1, filter2)
            else:
                return FilterOR(filter1, filter2)
        else:
            if filter_str == '':
                return FilterNONE()
            else:
                if 'not' in filter_str:
                    return FilterNOT.parse(filter_str)
                if ('<' in filter_str and
                        '>' in filter_str and
                        ' ' not in filter_str):
                    return FilterASPathRegex.parse(filter_str)
                elif '{' in filter_str and '}' in filter_str:
                    return FilterPrefixRange.parse(filter_str)
                elif 'any' == filter_str:
                    return FilterANY()
                elif 'as' in filter_str:
                    return FilterASN.parse(filter_str)
                elif 'peeras' == filter_str:
                    raise Exception('Not handled: %s' % filter_str)
                else:
                    raise Exception('Unknown or incorrect syntax filter %s' % filter_str)


class FilterOR(Filter):

    def __init__(self, filter1, filter2):
        self._filter1 = filter1
        self._filter2 = filter2

    def match(self, route):
        ret = self._filter1.match(route)
        if ret is not None:
            return ret
        else:
            return self._filter2.match(route)

    def __str__(self):
        return "%s(%s OR %s)" % (self.__class__.__name__, self._filter1, self._filter2)


class FilterAND(Filter):

    def __init__(self, filter1, filter2):
        self._filter1 = filter1
        self._filter2 = filter2

    def match(self, route):
        if self._filter1.match(route) and self._filter2.match(route):
            return route
        return None

    def __str__(self):
        return "%s(%s AND %s)" % (self.__class__.__name__, self._filter1, self._filter2)


class FilterNOT(Filter):
    def __init__(self, filter_):
        self._filter = filter_

    def match(self, route):
        if self._filter.match(route):
            return None
        return route

    @classmethod
    def parse(cls, line):
        # line example = 'NOT AS234'
        line = line.split('NOT')[1].strip()
        return Filter.parse(line)

    def __str__(self):
        return "%s(NOT %s)" % (self.__class__.__name__, self._filter)


class FilterANY(Filter):
    def match(self, route):
        return route

    def __str__(self):
        return "FilterANY()"


class FilterNONE(Filter):
    def match(self, route):
        return None

    def __str__(self):
        return "FilterNONE()"


class FilterASN(Filter):
    def __init__(self, asn):
        """
        asn = 12345
        """
        self.as_path = [asn]

    def match(self, route):
        #return self.as_path == route.as_path
        if self.as_path == route.as_path:
            return route
            #return self.idx
        else:
            return None

    @classmethod
    def parse(cls, line):
        # line example: 'AS234'
        asn = int(re.findall(r'\d+', line)[0])
        return cls(asn)

    def __str__(self):
        return "%s(%s)" % (self.__class__.__name__, self.as_path)


class FilterASPathRegex(Filter):
    def __init__(self, as_path_regex):
        self.as_path_regex = as_path_regex

    def match(self, route):
        if self.as_path_regex.contains(' '.join(map(str,route.as_path))):
            return route
        return None

    @classmethod
    def parse(cls, line):
        # line example '<^AS2 .* AS3$>
        line = line.replace('<', '').replace('>','')
        as_path_regex = ASPathRegex.parse(line)
        return cls(as_path_regex)

    def __str__(self):
        return "%s(%s)" % (self.__class__.__name__, self.as_path_regex)


class FilterPrefixRange(Filter):

    def __init__(self, prefix_range):
        self.prefix_range = prefix_range

    def match(self, route):
        if self.prefix_range.contains(route.prefix):
            return route
        return None

    @classmethod
    def parse(cls, line):
        # line example '{2.0.0.0/24, 3.0.0.0/20^28}'
        line = line.replace('{','').replace('}','')
        addr_prefix_set = PrefixSet.parse(line)
        return cls(addr_prefix_set)

    def __str__(self):
        return "%s(%s)" % (self.__class__.__name__, self.prefix_range)


class Action:
    """an action syntax:
    key = value or key.method(value). Ex:
    med = 100; pref=120; aspath.prepend(AS123); community .= {345:80}
    """

    def __init__(self, key=None, value=None):
        self.key = key
        self.value = value

    def apply(self, route):
        if self.key and self.value and self.key in route:
            route[self.key] = self.value

    @classmethod
    def parse(cls, line):
        if not line:
            return [Action()]
        line = line.strip().replace(' ','')
        actions = []
        for action_str in line.split(';'):
            action_cls = None
            if 'community' in action_str:
                action_cls = ActionCommunity
            elif 'aspath' in action_str:
                action_cls = ActionASpathPrepend
            if action_cls in [ActionCommunity, ActionASpathPrepend]:
                actions.append(action_cls.parse(line))
                continue

            if 'pref' in action_str:
                action_cls = ActionSetPref
            elif 'med' in action_str:
                action_cls = ActionSetMed
            elif 'origin' in action_str:
                action_cls = ActionSetOrigin
            elif 'nexthop' in action_str:
                action_cls = ActionSetNexthop
            action_key, action_value = action_str.split('=')
            actions.append(action_cls(action_key, action_value))
        return actions

    def __str__(self):
        return '<%s %s=%s>' % (self.__class__.__name__, self.key, self.value)


class ActionSetPref(Action):
    @classmethod
    def parse(cls, line):
        key, value = line.split('=')
        return cls(key, int(value))


class ActionSetMed(Action):
    pass


class ActionSetOrigin(Action):
    pass


class ActionSetNexthop(Action):
    pass


class ActionCommunity(Action):
    @classmethod
    def parse(cls, line):
        raise Exception('Not supported')


class ActionASpathPrepend(Action):
    def __init__(self, key, method, value):
        self.action_key = key
        self.action_value = value
        self.method = method

    @classmethod
    def parse(cls, line):
        key, method = line.split(';|.')
        try:
            method = int(method)
        except:
            pass
        instance = None
        if isinstance(method, int):
            instance = cls(key, 'set', method)
        elif 'prepend' in method:
            _, method = method.split('prepend')
            method = method.replace('(', '').replace(')', '')
            method = list(map(int, method.split(',')))
            instance = cls(key, 'prepend', method)
        return instance


class ASPathRegex:

    def __init__(self, regex):
        self.regex = regex

    @classmethod
    def parse(cls, line):
        """example: '<AS1>', '<^AS1>', '<AS2$>, '<^AS1 AS2 AS3$>', '<^AS1 .* AS2$>'
        """
        def to_regex_str(line):
            # turn 2* to (2(\s)*)*, 2{2,4} to (2(\s)*){2,4}, [1 2]{2} to (1 2(\s)*){2}
            for op in ['*', '?', '+', '{']:
                if op in line:
                    line = line.replace(op, '(\s)*)%s' % op)
                    break
            if '\s' not in line:
                line += '(\s)*)'
            line = '(' + line
            return line

        line = line.lower().replace('<', '').replace('>','').replace('as', '')
        parts = line.split()
        if len(parts) == 1:
            part = parts[0]
            if '^' not in part and '$' not in part:
                part = '^' + part + '$'
            elif '$' in part:
                part = '.*' + part
            return cls(re.compile(part))
        else:
            whole = ''
            for part in parts:
                # turn [2 3]{2} to (2(\s)*){2}|(3(\s)*){2}
                if '[' in part:
                    # turn [2 3]{2} to 2{2}, 3{2}, 2 3, 3 2
                    part = part.replace('[', '')
                    pref, suf = part.split(']')
                    comp = []
                    p = pref.split()
                    for i in p:
                        comp.append(to_regex_str(i+suf))
                    # TODO: much complex than I thought. Do it later
                    raise Exception('Not supported yet: %s' % part)
                whole += to_regex_str(part)
            return cls(re.compile(whole))
        return cls(None)

    def contains(self, aspath):
        # aspath = '1 2 300 300 300 400 400 500'
        #check if match the aspath
        line = ' '.join(aspath)
        if self.regex and self.regex.match(line):
            return True
        return False

    def __str__(self):
        return "%s" % self.regex
    __repr__ = __str__


class ASNumber(object):

    def __init__(self, asn):
        self.asn = asn

    @classmethod
    def parse(cls, line):
        asn = int(re.findall(r'\d+', line))
        return cls(asn)

    def __eq__(self, other):
        return self.asn == other.asn

    def __str__(self):
        return "AS%d" % self.asn

class IPv4Adress(ipaddress.IPv4Address):

    @classmethod
    def parse(cls, address):
        """a valid address (str) should look like: 128.9.128.5 """
        return cls(address)

class IPv4Prefix(ipaddress.IPv4Network):
    @classmethod
    def parse(cls, prefix):
        """a valid prefix (str) should look like: 128.9.128.5/32 """
        return cls(prefix)


class ASSet(object):
    def __init__(self, name, members=None):
        self.name = name
        self.members = members


class PrefixRange(object):
    """A prefix range representation in policy config.
    Ex. '{1.0.0.0/12^+}' means a range of prefixes from 1.0.0.0/12 to 1.0.0.0/32.
    """

    def __init__(self, prefix, n, m):
        self.prefix = IPv4Prefix(prefix)
        self.n = n # lower bound
        self.m = m # higher bound

    def __eq__(self, other):
        return (self.prefix == other.prefix and
                self.n == other.n and self.m == other.m)

    def __le__(self, other):
        return (self.prefix <= other.prefix)

    def contains(self, prefix):
        """Test if a prefix belongs to this range
        Args:
            prefix (str or ipaddress.IPv4Prefix): a prefix to be tested
        Returns:
            True if the prefix is within the range
        """
        return (prefix in self.prefix and
                prefix.prefixlen <= self.m and
                prefix.prefixlen >= self.n)

    @classmethod
    def parse(cls, line):
        """A valid prefix range should look like: '128.9.0.0/16', '128.6.0.0/16^-,
        '128.6.0.0/16^+', '128.6.0.0/16^20-24' or '128.6.0.0/20'
        """
        try:
            prefix = re.findall(r'\d+.\d+.\d+.\d+/\d+', line)[0]
            prefix = prefix.strip()
            prefix = ipaddress.ip_network(prefix)
            ops = re.split(r'\^', line)
            n = m = 0
            if len(ops) == 2:
                op = ops[1]
                if op == '-':
                    n = prefix.prefixlen - 1
                    m = 32 if prefix.version == 4 else 128
                elif op == '+':
                    n = prefix.prefixlen
                    m = 32 if prefix.version == 4 else 128
                elif '-' in op:
                    n,m = op.split('-')
                    n = int(n)
                    m = int(m)
                else:
                    n = int(op)
                    m = 32 if prefix.version == 4 else 128
            else:
                n = m = prefix.prefixlen
            return cls(prefix, n, m)
        except:
            raise TypeError('The prefix range has incorrect format %s' % line)

    def __hash__(self):
        return hash(frozenset((self.prefix, self.m, self.n)))

    def __str__(self):
        return "%s[prefix=%s, n=%d,m=%d]" % (self.__class__.__name__,
                self.prefix, self.n, self.m)
    __repr__ = __str__


class PrefixSet:

    def __init__(self, prefix_set):
        """prefix_set (set) is a set of prefix, e.g. ['1.0.0.0/20', '2.0.0.0/16']"""
        self.prefix_set = prefix_set

    def contains(self, prefix):
        for prefixrange in self.prefix_set:
            if prefixrange.contains(prefix):
                return True
        return False

    @classmethod
    def parse(cls, line):
        """line should look like: '{5.0.0.0/8^+, 128.9.0.0/16^-, 30.0.0.0/^24-28}'"""
        line = line.replace('{','').replace('}','').replace(' ','')
        range_set = set()
        for r in line.split(','):
            range_set.add(PrefixRange.parse(r))
        return cls(range_set)

    def __str__(self):
        return "%s[prefix set=[%s]]" % (self.__class__.__name__, self.prefix_set)

