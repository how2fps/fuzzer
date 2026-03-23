from parser import run_parser
res = run_parser(target='json-decoder', input_data=b'{"a": 1}')
closed = res.get('closed_result', {})
print('Closed result status:', closed.get('status'))
if 'branch_details_by_file' in closed:
    print('Found branch_details_by_file? YES')
    print('Number of edges:', sum(len(f.get('covered_branches', [])) for f in closed['branch_details_by_file']))
else:
    print('Found branch_details_by_file? NO')
