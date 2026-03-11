from nacl.encoding import HexEncoder
from nacl.signing import SigningKey

from identity import encode_public_key_bech32


def main() -> None:
    sk = SigningKey.generate()
    vk = sk.verify_key
    private_key = sk.encode(encoder=HexEncoder).decode()
    agent_id = vk.encode(encoder=HexEncoder).decode()
    print(f"private_key={private_key}")
    print(f"agent_id={agent_id}")
    print(f"agent_address={encode_public_key_bech32(agent_id)}")


if __name__ == "__main__":
    main()
