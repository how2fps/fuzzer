# Source Generated with Decompyle++
# File: ipv6_parser_stv.pyc (Python 3.12)

from collections import defaultdict
from buggy_ipyparse.ipv6_mstv import FunctionalBug, PerformanceBug, ReliabilityBug, InvalidityBug, IPv6
import os
import pandas as pd
import traceback
from datetime import datetime, UTC
from argparse import ArgumentParser, RawDescriptionHelpFormatter

def track_exception(exc = None):
    tb = exc.__traceback__
    last_frame = traceback.extract_tb(tb)[-1]
    bug_id = (type(exc), str(exc), last_frame.filename, last_frame.lineno)
    print('============================================================')
    print('TRACEBACK')
    print('============================================================')
    traceback.print_exc()
    print('============================================================')
    return bug_id


def log_full_traceback(exc, bug_type, log_dir, filename = ('logs', 'tracebacks.log')):
    '''
    Appends a full traceback to a log file for later analysis.
    '''
    os.makedirs(log_dir, exist_ok = True)
    log_path = os.path.join(log_dir, filename)
    timestamp = datetime.now(UTC)
# WARNING: Decompyle incomplete


def bug_count_to_csv(bug_count, output_path):
    rows = []
    if not bug_count:
        print('No bugs found. Skipping CSV creation')
        return None
    for key, count in bug_count.items():
        (bug_type, exc_type, exc_message, filename, lineno) = key
        rows.append({
            'bug_type': bug_type,
            'exc_type': exc_type.__name__,
            'exc_message': exc_message,
            'filename': filename,
            'lineno': lineno,
            'count': count })
    new_df = pd.DataFrame(rows)
    if os.path.exists(output_path):
        existing_df = pd.read_csv(output_path)
        combined_df = pd.concat([
            existing_df,
            new_df], ignore_index = True)
        combined_df = combined_df.groupby([
            'bug_type',
            'exc_type',
            'exc_message',
            'filename',
            'lineno'], as_index = False)['count'].sum()
    else:
        combined_df = new_df
    if not combined_df.empty:
        combined_df.to_csv(output_path, index = False)
        return None

if __name__ == '__main__':
    parser = ArgumentParser('ipv6 parser', description = 'Convert tokens that the parser has matched as an IPv6 address to a 128-bit number.', formatter_class = RawDescriptionHelpFormatter)
    parser.add_argument('--ipstr', help = 'The IP string to obtain the 128-bit integer from.')
    args = parser.parse_args()
    bug_count = defaultdict(int)
    print(f'''Running the IPv6 parser with ipstr: {args.ipstr}''')
    result = IPv6.parse_string(args.ipstr, parse_all = True)
    print(f'''Output: {result}''')
    logs_dir = 'logs'
    os.makedirs(logs_dir, exist_ok = True)
    csv_path = os.path.join(logs_dir, 'bug_counts.csv')
    bug_count_to_csv(bug_count, csv_path)
    print('Saved bug count report and tracebacks for the bugs encountered!')
    print(f'''Final bug count: {bug_count}''')
    return None
return None
# WARNING: Decompyle incomplete
