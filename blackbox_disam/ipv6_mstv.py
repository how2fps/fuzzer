# Source Generated with Decompyle++
# File: ipv6_mstv.pyc (Python 3.12)

from pyparsing import Group
from pyparsing import Literal
from pyparsing import ParserElement
from pyparsing import StringEnd
from pyparsing import StringStart
from pyparsing import Word
from argparse import ArgumentParser
import traceback
from ipv4_mstv import IPv4_in_IPv6
dwspc = ParserElement.DEFAULT_WHITE_CHARS
ParserElement.set_default_whitespace_chars('')

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


class ReliabilityBug(Exception):
    '''
    Error thrown in the presence of a Reliability bug.
    '''
    pass


class InvalidHexLength(Exception):
    '''
    Error thrown in the presence of an invalid Hex length.
    '''
    pass


class InvalidIPLength(Exception):
    '''
    Error thrown when the length of the IPv6 string is not the permitted length.
    '''
    pass


class InvalidityBug(Exception):
    '''
    Error throw in the presence of a Invalidity bug.
    '''
    pass


def convert_short(s, loc, toks):
    '''
    Convert part of an IPv6 address to a short integer.

    @param s: The original string
    @type s: str
    
    @param loc: The location in the string that the match occurred
    @type loc: int

    @param toks: The tokens that make up the IPv6 address part.
    @type toks: L{pyparsing.ParseResult}

    @return: The IPv6 address part expressed as a 16-bit integer.
    @rtype: int
    '''
    if len(toks[0]) > 4:
        raise InvalidityBug('Hex of length more than 4 found.')
    return [
        int(toks[0], 16)]


def convert_ipv6(s, loc, toks):
    '''
    Convert tokens that the parser has matched as an IPv6 address to a 128-bit number.

    @param s: The original string
    @type s: str
    
    @param loc: The location in the string that the match occurred
    @type loc: int

    @param toks: The tokens that make up the IPv6 address.  Individual
    parts of the address should already have been converted to
    integers, except for the MultiColons token which is handled
    specially.
    @type toks: L{pyparsing.ParseResult}

    @return: The IPv6 address expressed as a 128-bit integer.
    @rtype: int
    '''
    toks = toks[:]
    index = toks.index('::')
    toks = toks[:index] + [
        0] * ((8 - len(toks)) + 1) + toks[index + 1:]
    if len(toks) < 8:
        raise ReliabilityBug('Incorrect token length.')
    if ':::' in list(toks):
        raise ReliabilityBug("Invalid token(':::') parsed by pyparsing")
    if len(toks) > 8:
        raise InvalidityBug('Number of tokens passed exceeds the permitted number of hexes.')
    result = 0
    for tok in toks:
        result = (result << 16) + tok
    return [
        result]
# WARNING: Decompyle incomplete

G = Word('0123456789abcdefABCDEF', min = 1, max = 5).set_parse_action(convert_short)
Colon = Literal(':').suppress()
MultiColons = Literal('::') ^ Literal(':::')
IPv6 = (G + Colon[(7, 8)] + G ^ (G + Colon) * 6 + IPv4_in_IPv6 ^ G + (Colon + G) * (0, 6) + MultiColons ^ G + (Colon + G) * (0, 5) + MultiColons + G ^ G + (Colon + G) * (0, 4) + MultiColons + IPv4_in_IPv6 ^ G + (Colon + G) * (0, 4) + MultiColons + G + Colon + G ^ G + (Colon + G) * (0, 3) + MultiColons + G + Colon + IPv4_in_IPv6 ^ G + (Colon + G) * (0, 3) + MultiColons + (G + Colon) * 2 + G ^ G + (Colon + G) * (0, 2) + MultiColons + (G + Colon) * 2 + IPv4_in_IPv6 ^ G + (Colon + G) * (0, 2) + MultiColons + (G + Colon) * 3 + G ^ G + (Colon + G) * (0, 1) + MultiColons + (G + Colon) * 3 + IPv4_in_IPv6 ^ G + (Colon + G) * (0, 1) + MultiColons + (G + Colon) * 4 + G ^ G + MultiColons + (G + Colon) * 5 + G ^ MultiColons + (G + Colon) * (0, 6) + (G ^ IPv4_in_IPv6)).set_parse_action(convert_ipv6)
IPv6_WholeString = StringStart() + IPv6 + StringEnd()
ParserElement.set_default_whitespace_chars(dwspc)
