import socket, sys
port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", port))
print(f"UDP echo server on port {port}")
while True:
    data, addr = s.recvfrom(2048)
    s.sendto(data, addr)
