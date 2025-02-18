# Copyright (c) Aptos
# SPDX-License-Identifier: Apache-2.0

import time
from typing import Any, Dict, List, Optional

import httpx
from account import Account
from account_address import AccountAddress
from authenticator import (Authenticator, Ed25519Authenticator,
                           MultiAgentAuthenticator)
from bcs import Serializer
from transactions import (MultiAgentRawTransaction, RawTransaction,
                          ScriptFunction, SignedTransaction,
                          TransactionArgument, TransactionPayload)
from type_tag import StructTag, TypeTag

TESTNET_URL = "https://fullnode.devnet.aptoslabs.com"
FAUCET_URL = "https://faucet.devnet.aptoslabs.com"


class RestClient:
    """A wrapper around the Aptos-core Rest API"""

    chain_id: int
    client: httpx.Client
    base_url: str

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.client = httpx.Client()
        self.chain_id = int(self.info()["chain_id"])

    #
    # Account accessors
    #

    def account(self, account_address: AccountAddress) -> Dict[str, str]:
        """Returns the sequence number and authentication key for an account"""

        response = self.client.get(f"{self.base_url}/accounts/{account_address}")
        assert response.status_code == 200, f"{response.text} - {account_address}"
        return response.json()

    def account_balance(self, account_address: str) -> int:
        """Returns the test coin balance associated with the account"""
        return self.account_resource(
            account_address, "0x1::Coin::CoinStore<0x1::TestCoin::TestCoin>"
        )["data"]["coin"]["value"]

    def account_sequence_number(self, account_address: AccountAddress) -> int:
        account_res = self.account(account_address)
        return int(account_res["sequence_number"])

    def account_resource(
        self, account_address: AccountAddress, resource_type: str
    ) -> Optional[Dict[str, Any]]:
        response = self.client.get(
            f"{self.base_url}/accounts/{account_address}/resource/{resource_type}"
        )
        if response.status_code == 404:
            return None
        assert response.status_code == 200, response.text
        return response.json()

    def get_table_item(
        self, handle: str, key_type: str, value_type: str, key: Any
    ) -> Any:
        response = self.client.post(
            f"{self.base_url}/tables/{handle}/item",
            json={
                "key_type": key_type,
                "value_type": value_type,
                "key": key,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    #
    # Ledger accessors
    #

    def info(self) -> Dict[str, str]:
        response = self.client.get(self.base_url)
        assert response.status_code == 200, f"{response.text}"
        return response.json()

    #
    # Transactions
    #

    def submit_bcs_transaction(self, signed_transaction: SignedTransaction) -> str:
        headers = {"Content-Type": "application/x.aptos.signed_transaction+bcs"}
        response = self.client.post(
            f"{self.base_url}/transactions",
            headers=headers,
            content=signed_transaction.bytes(),
        )
        assert response.status_code == 202, f"{response.text} - {signed_transaction}"
        return response.json()["hash"]

    def submit_transaction(self, sender: Account, payload: Dict[str, Any]) -> str:
        """
        1) Generates a transaction request
        2) submits that to produce a raw transaction
        3) signs the raw transaction
        4) submits the signed transaction
        """

        txn_request = {
            "sender": f"{sender.address()}",
            "sequence_number": str(self.account_sequence_number(sender.address())),
            "max_gas_amount": "2000",
            "gas_unit_price": "1",
            "expiration_timestamp_secs": str(int(time.time()) + 600),
            "payload": payload,
        }

        res = self.client.post(
            f"{self.base_url}/transactions/signing_message", json=txn_request
        )
        assert res.status_code == 200, res.text

        to_sign = bytes.fromhex(res.json()["message"][2:])
        signature = sender.sign(to_sign)
        txn_request["signature"] = {
            "type": "ed25519_signature",
            "public_key": f"{sender.public_key()}",
            "signature": f"{signature}",
        }

        headers = {"Content-Type": "application/json"}
        response = self.client.post(
            f"{self.base_url}/transactions", headers=headers, json=txn_request
        )
        assert response.status_code == 202, f"{response.text} - {txn}"
        return response.json()["hash"]

    def transaction_pending(self, txn_hash: str) -> bool:
        response = self.client.get(f"{self.base_url}/transactions/{txn_hash}")
        if response.status_code == 404:
            return True
        assert response.status_code == 200, f"{response.text} - {txn_hash}"
        return response.json()["type"] == "pending_transaction"

    def wait_for_transaction(self, txn_hash: str) -> None:
        """Waits up to 10 seconds for a transaction to move past pending state."""

        count = 0
        while self.transaction_pending(txn_hash):
            assert count < 10, f"transaction {txn_hash} timed out"
            time.sleep(1)
            count += 1
        response = self.client.get(f"{self.base_url}/transactions/{txn_hash}")
        assert "success" in response.json(), f"{response.text} - {txn_hash}"

    #
    # Transaction helpers
    #

    def create_multi_agent_bcs_transaction(
        self,
        sender: Account,
        secondary_accounts: List[Account],
        payload: TransactionPayload,
    ) -> SignedTransaction:
        raw_transaction = MultiAgentRawTransaction(
            RawTransaction(
                sender.address(),
                self.account_sequence_number(sender.address()),
                payload,
                2000,
                1,
                int(time.time()) + 600,
                self.chain_id,
            ),
            [x.address() for x in secondary_accounts],
        )

        keyed_txn = raw_transaction.keyed()

        authenticator = Authenticator(
            MultiAgentAuthenticator(
                Authenticator(
                    Ed25519Authenticator(sender.public_key(), sender.sign(keyed_txn))
                ),
                [
                    (
                        x.address(),
                        Authenticator(
                            Ed25519Authenticator(x.public_key(), x.sign(keyed_txn))
                        ),
                    )
                    for x in secondary_accounts
                ],
            )
        )

        return SignedTransaction(raw_transaction.inner(), authenticator)

    def create_single_signer_bcs_transaction(
        self, sender: Account, payload: TransactionPayload
    ) -> SignedTransaction:
        raw_transaction = RawTransaction(
            sender.address(),
            self.account_sequence_number(sender.address()),
            payload,
            2000,
            1,
            int(time.time()) + 600,
            self.chain_id,
        )

        signature = sender.sign(raw_transaction.keyed())
        authenticator = Authenticator(
            Ed25519Authenticator(sender.public_key(), signature)
        )
        return SignedTransaction(raw_transaction, authenticator)

    #
    # Transaction wrappers
    #

    def transfer(self, sender: Account, recipient: AccountAddress, amount: int) -> str:
        """Transfer a given coin amount from a given Account to the recipient's account address.
        Returns the sequence number of the transaction used to transfer."""

        payload = {
            "type": "script_function_payload",
            "function": "0x1::Coin::transfer",
            "type_arguments": ["0x1::TestCoin::TestCoin"],
            "arguments": [
                f"{recipient}",
                str(amount),
            ],
        }
        return self.submit_transaction(sender, payload)

    def bcs_transfer(
        self, sender: Account, recipient: AccountAddress, amount: int
    ) -> str:
        transaction_arguments = [
            TransactionArgument(recipient, Serializer.struct),
            TransactionArgument(amount, Serializer.u64),
        ]

        payload = ScriptFunction.natural(
            "0x1::Coin",
            "transfer",
            [TypeTag(StructTag.from_str("0x1::TestCoin::TestCoin"))],
            transaction_arguments,
        )

        signed_transaction = self.create_single_signer_bcs_transaction(
            sender, TransactionPayload(payload)
        )
        return self.submit_bcs_transaction(signed_transaction)

    #
    # Token transaction wrappers
    #

    def create_collection(
        self, account: Account, name: str, description: str, uri: str
    ) -> str:
        """Creates a new collection within the specified account"""

        transaction_arguments = [
            TransactionArgument(name, Serializer.str),
            TransactionArgument(description, Serializer.str),
            TransactionArgument(uri, Serializer.str),
        ]

        payload = ScriptFunction.natural(
            "0x1::Token",
            "create_unlimited_collection_script",
            [],
            transaction_arguments,
        )

        signed_transaction = self.create_single_signer_bcs_transaction(
            account, TransactionPayload(payload)
        )
        return self.submit_bcs_transaction(signed_transaction)

    def create_token(
        self,
        account: Account,
        collection_name: str,
        name: str,
        description: str,
        supply: int,
        uri: str,
        royalty_points_per_million: int,
    ) -> str:
        transaction_arguments = [
            TransactionArgument(collection_name, Serializer.str),
            TransactionArgument(name, Serializer.str),
            TransactionArgument(description, Serializer.str),
            TransactionArgument(True, Serializer.bool),
            TransactionArgument(supply, Serializer.u64),
            TransactionArgument(uri, Serializer.str),
            TransactionArgument(royalty_points_per_million, Serializer.u64),
        ]

        payload = ScriptFunction.natural(
            "0x1::Token",
            "create_unlimited_token_script",
            [],
            transaction_arguments,
        )
        signed_transaction = self.create_single_signer_bcs_transaction(
            account, TransactionPayload(payload)
        )
        return self.submit_bcs_transaction(signed_transaction)

    def offer_token(
        self,
        account: Account,
        receiver: str,
        creator: str,
        collection_name: str,
        token_name: str,
        amount: int,
    ) -> str:
        transaction_arguments = [
            TransactionArgument(receiver, Serializer.struct),
            TransactionArgument(creator, Serializer.struct),
            TransactionArgument(collection_name, Serializer.str),
            TransactionArgument(token_name, Serializer.str),
            TransactionArgument(amount, Serializer.u64),
        ]

        payload = ScriptFunction.natural(
            "0x1::TokenTransfers",
            "offer_script",
            [],
            transaction_arguments,
        )
        signed_transaction = self.create_single_signer_bcs_transaction(
            account, TransactionPayload(payload)
        )
        return self.submit_bcs_transaction(signed_transaction)

    def claim_token(
        self,
        account: Account,
        sender: str,
        creator: str,
        collection_name: str,
        token_name: str,
    ) -> str:
        transaction_arguments = [
            TransactionArgument(sender, Serializer.struct),
            TransactionArgument(creator, Serializer.struct),
            TransactionArgument(collection_name, Serializer.str),
            TransactionArgument(token_name, Serializer.str),
        ]

        payload = ScriptFunction.natural(
            "0x1::TokenTransfers",
            "claim_script",
            [],
            transaction_arguments,
        )
        signed_transaction = self.create_single_signer_bcs_transaction(
            account, TransactionPayload(payload)
        )
        return self.submit_bcs_transaction(signed_transaction)

    def direct_transfer_token(
        self,
        sender: Account,
        receiver: Account,
        creators_address: AccountAddress,
        collection_name: str,
        token_name: str,
        amount: int,
    ) -> str:
        transaction_arguments = [
            TransactionArgument(creators_address, Serializer.struct),
            TransactionArgument(collection_name, Serializer.str),
            TransactionArgument(token_name, Serializer.str),
            TransactionArgument(amount, Serializer.u64),
        ]

        payload = ScriptFunction.natural(
            "0x1::Token",
            "direct_transfer_script",
            [],
            transaction_arguments,
        )

        signed_transaction = self.create_multi_agent_bcs_transaction(
            sender,
            [receiver],
            TransactionPayload(payload),
        )
        return self.submit_bcs_transaction(signed_transaction)

    #
    # Token accessors
    #

    def get_token_balance(
        self,
        owner: AccountAddress,
        creator: AccountAddress,
        collection_name: str,
        token_name: str,
    ) -> Any:
        token_store = self.account_resource(owner, "0x1::Token::TokenStore")["data"][
            "tokens"
        ]["handle"]

        token_id = {
            "creator": creator.hex(),
            "collection": collection_name,
            "name": token_name,
        }

        return self.get_table_item(
            token_store,
            "0x1::Token::TokenId",
            "0x1::Token::Token",
            token_id,
        )["value"]

    def get_token_data(
        self, creator: AccountAddress, collection_name: str, token_name: str
    ) -> Any:
        token_data = self.account_resource(creator, "0x1::Token::Collections")["data"][
            "token_data"
        ]["handle"]

        token_id = {
            "creator": creator.hex(),
            "collection": collection_name,
            "name": token_name,
        }

        return self.get_table_item(
            token_data,
            "0x1::Token::TokenId",
            "0x1::Token::TokenData",
            token_id,
        )

    def get_collection(self, creator: AccountAddress, collection_name: str) -> Any:
        token_data = self.account_resource(creator, "0x1::Token::Collections")["data"][
            "collections"
        ]["handle"]

        return self.get_table_item(
            token_data,
            "0x1::ASCII::String",
            "0x1::Token::Collection",
            collection_name,
        )


class FaucetClient:
    """Faucet creates and funds accounts. This is a thin wrapper around that."""

    base_url: str
    rest_client: RestClient

    def __init__(self, base_url: str, rest_client: RestClient):
        self.base_url = base_url
        self.rest_client = rest_client

    def fund_account(self, address: str, amount: int):
        """This creates an account if it does not exist and mints the specified amount of
        coins into that account."""
        txns = self.rest_client.client.post(
            f"{self.base_url}/mint?amount={amount}&address={address}"
        )
        assert txns.status_code == 200, txns.text
        for txn_hash in txns.json():
            self.rest_client.wait_for_transaction(txn_hash)


def coin_transfer():
    rest_client = RestClient(TESTNET_URL)
    faucet_client = FaucetClient(FAUCET_URL, rest_client)

    alice = Account.generate()
    bob = Account.generate()

    print("\n=== Addresses ===")
    print(f"Alice: {alice.address()}")
    print(f"Bob: {bob.address()}")

    faucet_client.fund_account(alice.address(), 1_000_000)
    faucet_client.fund_account(bob.address(), 0)

    print("\n=== Initial Balances ===")
    print(f"Alice: {rest_client.account_balance(alice.address())}")
    print(f"Bob: {rest_client.account_balance(bob.address())}")

    # Have Alice give Bob 1_000 coins
    txn_hash = rest_client.transfer(alice, bob.address(), 1_000)
    rest_client.wait_for_transaction(txn_hash)

    print("\n=== Intermediate Balances ===")
    print(f"Alice: {rest_client.account_balance(alice.address())}")
    print(f"Bob: {rest_client.account_balance(bob.address())}")

    # Have Alice give Bob another 1_000 coins using BCS
    txn_hash = rest_client.bcs_transfer(alice, bob.address(), 1_000)
    rest_client.wait_for_transaction(txn_hash)

    print("\n=== Final Balances ===")
    print(f"Alice: {rest_client.account_balance(alice.address())}")
    print(f"Bob: {rest_client.account_balance(bob.address())}")


def token_transfer():
    rest_client = RestClient(TESTNET_URL)
    faucet_client = FaucetClient(FAUCET_URL, rest_client)

    alice = Account.generate()
    bob = Account.generate()

    collection_name = "Alice's"
    token_name = "Alice's first token"

    print("\n=== Addresses ===")
    print(f"Alice: {alice.address()}")
    print(f"Bob: {bob.address()}")

    faucet_client.fund_account(alice.address(), 10_000_000)
    faucet_client.fund_account(bob.address(), 10_000_000)

    print("\n=== Initial Balances ===")
    print(f"Alice: {rest_client.account_balance(alice.address())}")
    print(f"Bob: {rest_client.account_balance(bob.address())}")

    print("\n=== Creating Collection and Token ===")

    txn_hash = rest_client.create_collection(
        alice, collection_name, "Alice's simple collection", "https://aptos.dev"
    )
    rest_client.wait_for_transaction(txn_hash)

    txn_hash = rest_client.create_token(
        alice,
        collection_name,
        token_name,
        "Alice's simple token",
        1,
        "https://aptos.dev/img/nyan.jpeg",
        0,
    )
    rest_client.wait_for_transaction(txn_hash)

    print(
        f"Alice's collection: {rest_client.get_collection(alice.address(), collection_name)}"
    )
    print(
        f"Alice's token balance: {rest_client.get_token_balance(alice.address(), alice.address(), collection_name, token_name)}"
    )
    print(
        f"Alice's token data: {rest_client.get_token_data(alice.address(), collection_name, token_name)}"
    )

    print("\n=== Transferring the token to Bob ===")
    txn_hash = rest_client.offer_token(
        alice, bob.address(), alice.address(), collection_name, token_name, 1
    )
    rest_client.wait_for_transaction(txn_hash)

    txn_hash = rest_client.claim_token(
        bob, alice.address(), alice.address(), collection_name, token_name
    )
    rest_client.wait_for_transaction(txn_hash)

    print(
        f"Alice's token balance: {rest_client.get_token_balance(alice.address(), alice.address(), collection_name, token_name)}"
    )
    print(
        f"Bob's token balance: {rest_client.get_token_balance(bob.address(), alice.address(), collection_name, token_name)}"
    )

    print("\n=== Transferring the token back to Alice using MultiAgent ===")
    txn_hash = rest_client.direct_transfer_token(
        bob, alice, alice.address(), collection_name, token_name, 1
    )
    rest_client.wait_for_transaction(txn_hash)

    print(
        f"Alice's token balance: {rest_client.get_token_balance(alice.address(), alice.address(), collection_name, token_name)}"
    )
    print(
        f"Bob's token balance: {rest_client.get_token_balance(bob.address(), alice.address(), collection_name, token_name)}"
    )


if __name__ == "__main__":
    coin_transfer()
    token_transfer()
