with open(r"D:\Backup\Documents\New project\.agents\skills\hertzflow\alpha\INSTRUCTIONS.md", "r", encoding="utf-8") as f:
    lines = f.readlines()
for i in range(100, 160):
    if i < len(lines):
        print(f"{i+1}: {lines[i]}", end="")
