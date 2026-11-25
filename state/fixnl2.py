import pathlib
p = pathlib.Path("tests/host.c")
s = p.read_text(encoding="utf-8")
nl = chr(10)
bs = chr(92)
# any PUT("<literal-newline>") becomes PUT("\n")
s = s.replace('PUT("' + nl + '")', 'PUT("' + bs + 'n")')
p.write_text(s, encoding="utf-8")
print(s.count('PUT("' + nl + '")'), "literal newlines left")
