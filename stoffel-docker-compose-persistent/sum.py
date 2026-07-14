import sys

def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} N", file=sys.stderr)
        sys.exit(1)

    n = int(sys.argv[1])
    total = sum(n * i for i in range(1, n + 1)) % (2 ** 64)
    print(total)

if __name__ == "__main__":
    main()
