import socket, time, json, sys, statistics

# usage: udp_probe.py <dst-ip> [count] [interval_s] [timeout_s] [outfile]
# Backward compatible with the D1 version (count only).
if len(sys.argv) < 2:
    sys.exit("usage: udp_probe.py <dst-ip> [count] [interval_s] [timeout_s] [outfile]")
dst = sys.argv[1]
try:
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
    timeout = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
except ValueError:
    sys.exit("count must be an integer; interval and timeout must be numbers")
outfile = sys.argv[5] if len(sys.argv) > 5 else "/tmp/probe_result.json"
port = 9999

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(timeout)

rtts, lost, errors, samples = [], [], [], []

for seq in range(count):
    t0 = time.perf_counter()
    t_wall = time.time()
    rtt = None
    try:
        s.sendto(f"{seq}|{t_wall}".encode(), (dst, port))
    except OSError as e:  # e.g. network unreachable: count as lost
        lost.append(seq)
        errors.append(str(e))
        samples.append({"seq": seq, "t": t_wall, "rtt_ms": None})
        time.sleep(interval)
        continue
    try:
        while True:
            data, _ = s.recvfrom(2048)
            try:
                if int(data.split(b"|")[0]) == seq:
                    break
            except ValueError:  # ignore malformed / stray packets
                continue
            # stale reply from an earlier probe: keep waiting
        rtt = (time.perf_counter() - t0) * 1000
        rtts.append(rtt)
    except socket.timeout:
        lost.append(seq)
    except OSError as e:  # e.g. ICMP error surfaced by the OS
        lost.append(seq)
        errors.append(str(e))
    samples.append({"seq": seq, "t": t_wall,
                    "rtt_ms": round(rtt, 3) if rtt is not None else None})
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
    "errors": sorted(set(errors)),
    "interval_s": interval, "timeout_s": timeout,
    "samples": samples,
}

print(json.dumps({k: v for k, v in result.items() if k != "samples"}, indent=2))
json.dump(result, open(outfile, "w"))
