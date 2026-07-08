with open(r"D:\Backup\Documents\New project\main.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

# Find the main loop - after tickers check, add self-ping via external URL
for i, line in enumerate(lines):
    if "self.health_guard.feed_data()" in line and i > 200:
        # Insert self-ping after this line
        indent = "                "
        ping_block = [
            indent + "# Keep-alive: ping self via external URL\n",
            indent + "try:\n",
            indent + "    import requests as _r\n",
            indent + "    _r.get(\"https://gate-monitor-1.onrender.com/\", timeout=5)\n",
            indent + "except Exception:\n",
            indent + "    pass\n",
        ]
        for j, bl in enumerate(ping_block):
            lines.insert(i + 1 + j, bl)
        print(f"Inserted self-ping after L{i+1}")
        break

# Add requests import at top
for i, line in enumerate(lines):
    if "import threading" in line:
        lines.insert(i + 1, "import requests\n")
        print("Added requests import")
        break

with open(r"D:\Backup\Documents\New project\main.py", "w", encoding="utf-8") as f:
    f.writelines(lines)
print("Done")
