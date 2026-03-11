from nacl.encoding import HexEncoder
from nacl.signing import SigningKey


def main() -> None:
    sk = SigningKey.generate()
    vk = sk.verify_key
    print(f"relay_private_key={sk.encode(encoder=HexEncoder).decode()}")
    print(f"relay_id={vk.encode(encoder=HexEncoder).decode()}")


if __name__ == "__main__":
    main()
