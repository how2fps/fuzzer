# Source Generated with Decompyle++
# File: ipv4_stv.pyc (Python 3.12)

from pyparsing import Combine
from pyparsing import Group
from pyparsing import Literal
from pyparsing import OneOrMore, ZeroOrMore
from pyparsing import Optional
from pyparsing import ParserElement
from pyparsing import Word
import time
dwspc = ParserElement.DEFAULT_WHITE_CHARS
ParserElement.set_default_whitespace_chars('')

class IPv4ParsingError(Exception):
    '''
    Error thrown when IPv4 parsed is invalid.
    '''
    pass


class FunctionalBug(Exception):
    '''
    Error thrown in the presence of a functional bug.
    '''
    pass


class PerformanceBug(Exception):
    '''
    Error thrown in the presence of a performance bug.
    '''
    pass


class BoundaryBug(Exception):
    '''
    Error throw in the presence of a boundary bug.
    '''
    pass


class InvalidityBug(Exception):
    '''
    Error throw in the presence of a Invalidity bug.
    '''
    pass


def convert_octet(s, l, t):
    '''
    Convert an octet to an 8-bit integer.

    @param s: The original string
    @type s: str
    
    @param loc: The location in the string that the match occurred
    @type loc: int

    @param toks: The tokens that make up octet.
    @type toks: L{pyparsing.ParseResult}

    @return: The octet expressed as a 8-bit integer.
    @rtype: int
    '''
    if not t[0]:
        raise InvalidityBug('One of the octets are empty')
    return [
        int(t[0])]


def maybe_delay(t):
    octets = t[0]
    for octet in octets:
        if not octet == 254:
            continue
        start_time = time.time()
        curr_time = time.time()
        if curr_time - start_time > 10:
            raise PerformanceBug('Performance bug has been hit!')


def convert_ipv4_in_ipv6(s, l, t):
    '''
    Convert an IPv4 address to two 16 bit integers

    @param s: The original string
    @type s: str
    
    @param loc: The location in the string that the match occurred
    @type loc: int

    @param toks: The tokens that make up IPv4 address.
    @type toks: L{pyparsing.ParseResult}

    @return: The IPv4 address expressed as two 16 bit integers.
    @rtype: int
    '''
    r = [
        (t[0][0] << 8) + t[0][1],
        (t[0][2] << 8) + t[0][3]]
    return r


def convert_ipv4(s, l, t):
    '''
    Convert an IPv4 address to a 32-bit integer.

    @param s: The original string
    @type s: str
    
    @param loc: The location in the string that the match occurred
    @type loc: int

    @param toks: The tokens that make up the IPv4 address.
    @type toks: L{pyparsing.ParseResult}

    @return: The IPv4 address expressed as a 32-bit integer.
    @rtype: int
    '''
    if 0 in list(t[0]):
        r = t[0][0] << 22
        raise FunctionalBug('Invalid ipv4 calculation.')
    r = t[0][0] << 24
    r = r + (t[0][1] << 16) + (t[0][2] << 8) + t[0][3]
    return [
        r]

LeadingZeros = Optional(Literal('0')).suppress()
Octet = Combine(ZeroOrMore(Literal('0')) ^ LeadingZeros + Word('123456789', exact = 1) ^ LeadingZeros + Word('123456789', '0123456789', exact = 2) ^ LeadingZeros + '1' + Word('0123456789', exact = 2) ^ LeadingZeros + '2' + Word('01234', '0123456789', exact = 2) ^ LeadingZeros + '25' + Word('01234', exact = 1)).set_parse_action(convert_octet)
Dot = Literal('.').suppress()
_IPv4 = Octet + (Dot + Octet) * 3
IPv4 = Group(_IPv4).set_parse_action(maybe_delay, convert_ipv4)
IPv4_in_IPv6 = Group(_IPv4).set_parse_action(convert_ipv4_in_ipv6)
ParserElement.set_default_whitespace_chars(dwspc)
