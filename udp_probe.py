import socket, time, json, sys, statistics

dst   = sys.argv[1]
count = int(sys.argv[2]) if len(sys.argv) > 2 else 50
port, timeout, interval = 9999, 1.0, 0.1

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(timeout)

rtts, lost = [], []
for seq in range(count):
    t0 = time.perf_counter()
    s.sendto(f"{seq}|{time.time()}".encode(), (dst, port))
    try:
        while True:
            data, _ = s.recvfrom(2048)
            if int(data.split(b"|")[0]) == seq:
                break
        rtts.append((time.perf_counter() - t0) * 1000)
    except socket.timeout:
        lost.append(seq)
    time.sleep(interval)

jitter = (statistics.mean(abs(a - b) for a, b in zip(rtts, rtts[1:]))
          if len(rtts) > 1 else 0)
result = {
    "dst": dst, "sent": count, "received": len(rtts),
    "loss_pct": round(100 * len(lost) / count, 2),
    "rtt_min_ms": round(min(rtts), 2) if rtts else None,
    "rtt_avg_ms": round(statistics.mean(rtts), 2) if rtts else None,
    "rtt_max_ms": round(max(rtts), 2) if rtts else None,
    "jitter_ms": round(jitter, 2),
    "lost_seq": lost,
}
print(json.dumps(result, indent=2))
json.dump(result, open("/tmp/probe_result.json", "w"))
