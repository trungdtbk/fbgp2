
class Policy(object):
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


class Filter(object):
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


class Action(object):
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

