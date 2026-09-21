# Testa como Python interpreta \d em strings

# Forma 1: 1 backslash no código (como está no app.py agora)
s1 = '(\d{2})(0[1-9]|1[0-2])'
print(f's1: {repr(s1)}')
print(f's1 caracteres: {list(s1)}')

# Forma 2: raw string com 1 backslash
s2 = r'(\d{2})(0[1-9]|1[0-2])'
print(f's2 (raw): {repr(s2)}')
print(f's2 caracteres: {list(s2)}')

# Forma 3: 2 backslashes no código
s3 = '(\\d{2})(0[1-9]|1[0-2])'
print(f's3 (2 bs): {repr(s3)}')
print(f's3 caracteres: {list(s3)}')

# Mostra bytes
print('\nBytes:')
print(f's1 bytes: {s1.encode("utf-8")}')
print(f's2 bytes: {s2.encode("utf-8")}')
print(f's3 bytes: {s3.encode("utf-8")}')
