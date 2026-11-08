"""Assert an ELF object targets the given machine (aarch64=183). Usage: check_elf.py FILE aarch64"""
import struct, sys
MACH = {"aarch64": 183, "arm": 40, "x86_64": 62}
data = open(sys.argv[1], "rb").read(64)
assert data[:4] == b"\x7fELF", "not an ELF file"
e_machine = struct.unpack_from("<H", data, 18)[0]
want = MACH[sys.argv[2]]
print(f"e_machine={e_machine} want={want} class={'64' if data[4] == 2 else '32'}-bit")
sys.exit(0 if e_machine == want else 1)
