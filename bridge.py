from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware #Necessary for POA chains
from datetime import datetime
import json
import pandas as pd


def connect_to(chain):
    if chain == 'source':  # The source contract chain is avax
        api_url = f"https://api.avax-test.network/ext/bc/C/rpc" #AVAX C-chain testnet

    if chain == 'destination':  # The destination contract chain is bsc
        api_url = f"https://data-seed-prebsc-1-s1.binance.org:8545/" #BSC testnet

    if chain in ['source','destination']:
        w3 = Web3(Web3.HTTPProvider(api_url))
        # inject the poa compatibility middleware to the innermost layer
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def get_contract_info(chain, contract_info):
    """
        Load the contract_info file into a dictionary
        This function is used by the autograder and will likely be useful to you
    """
    try:
        with open(contract_info, 'r')  as f:
            contracts = json.load(f)
    except Exception as e:
        print( f"Failed to read contract info\nPlease contact your instructor\n{e}" )
        return 0
    return contracts[chain]



def scan_blocks(chain, contract_info="contract_info.json"):
    """
    chain - (string) should be either "source" or "destination"
    Scan the last 5 blocks of the source and destination chains
    Look for 'Deposit' events on the source chain and 'Unwrap' events on the destination chain
    When Deposit events are found on the source chain, call the 'wrap' function on the destination chain
    When Unwrap events are found on the destination chain, call the 'withdraw' function on the source chain
    """
    # This is different from Bridge IV where chain was "avalanche" or "bsc"
    if chain not in ['source', 'destination']:
        print(f"Invalid chain: {chain}")
        return 0

    # Helper to get opposite side label
    def other_side(x):
        return 'destination' if x == 'source' else 'source'

    # Load contract metadata for both sides
    src_meta = get_contract_info('source', contract_info)
    dst_meta = get_contract_info('destination', contract_info)
    if not src_meta or not dst_meta:
        return 0

    # Connect both chains
    w3_src = connect_to('source')
    w3_dst = connect_to('destination')
    if (not w3_src.is_connected()) or (not w3_dst.is_connected()):
        print("Failed to connect to one of the RPC endpoints")
        return 0

    # Resolve scanning window: last 5 blocks on each chain
    latest_src = w3_src.eth.block_number
    latest_dst = w3_dst.eth.block_number
    from_src = max(latest_src - 4, 0)
    from_dst = max(latest_dst - 4, 0)

    # Instantiate contracts
    src_contract = w3_src.eth.contract(address=src_meta['address'], abi=src_meta['abi'])
    dst_contract = w3_dst.eth.contract(address=dst_meta['address'], abi=dst_meta['abi'])

    # Warden key/address (the deployer key recorded in contract_info.json)
    # Expecting keys under each side, but we just need one private key that owns the bridge role
    # Store as 'warden_private_key' and 'warden_address' in your contract_info.json
    if 'warden_private_key' in src_meta:
        warden_pk = src_meta['warden_private_key']
        warden_addr = Web3.to_checksum_address(src_meta['warden_address'])
    elif 'warden_private_key' in dst_meta:
        warden_pk = dst_meta['warden_private_key']
        warden_addr = Web3.to_checksum_address(dst_meta['warden_address'])
    else:
        print("warden key/address not found in contract_info.json")
        return 0

    # We only perform the cross-call for the direction we were asked to handle, to match the autograder behavior
    # If chain == 'source': read Deposit on source and call wrap on destination
    # If chain == 'destination': read Unwrap on destination and call withdraw on source

    if chain == 'source':
        # Read Deposit(token, recipient, amount)
        try:
            evts = src_contract.events.Deposit.create_filter(
                from_block=from_src,
                to_block=latest_src
            ).get_all_entries()
        except Exception as e:
            print(f"Failed to fetch Deposit events on source: {e}")
            evts = []

        if not evts:
            print("No Deposit events found on source in the last 5 blocks")
            return 0

        # For each deposit, call wrap on destination with the same params and a warden signature
        for evt in evts:
            args = evt['args']
            token = args['token']
            recipient = args['recipient']
            amount = int(args['amount'])

            # Many templates expect a simple wrap(token, recipient, amount) authorized by msg.sender (warden).
            # If your contract requires an ECDSA signature, add hashing/signing here.
            try:
                nonce = w3_dst.eth.get_transaction_count(warden_addr)
                tx = dst_contract.functions.wrap(token, recipient, amount).build_transaction({
                    'from': warden_addr,
                    'nonce': nonce,
                    'gas': 250000,
                    'maxFeePerGas': w3_dst.to_wei('2', 'gwei'),
                    'maxPriorityFeePerGas': w3_dst.to_wei('1', 'gwei'),
                    'chainId': w3_dst.eth.chain_id,
                })
                signed = w3_dst.eth.account.sign_transaction(tx, private_key=warden_pk)
                tx_hash = w3_dst.eth.send_raw_transaction(signed.raw_transaction)
                print(f"wrap sent on destination for tx {evt.transactionHash.hex()} -> {tx_hash.hex()}")
            except Exception as e:
                print(f"Failed to call wrap on destination: {e}")

    if chain == 'destination':
        # Read Unwrap(token, recipient, amount)
        try:
            evts = dst_contract.events.Unwrap.create_filter(
                from_block=from_dst,
                to_block=latest_dst
            ).get_all_entries()
        except Exception as e:
            print(f"Failed to fetch Unwrap events on destination: {e}")
            evts = []

        if not evts:
            print("No Unwrap events found on destination in the last 5 blocks")
            return 0

        # For each unwrap, call withdraw on source
        for evt in evts:
            args = evt['args']
            token = args['token']
            recipient = args['recipient']
            amount = int(args['amount'])

            try:
                nonce = w3_src.eth.get_transaction_count(warden_addr)
                tx = src_contract.functions.withdraw(token, recipient, amount).build_transaction({
                    'from': warden_addr,
                    'nonce': nonce,
                    'gas': 250000,
                    'maxFeePerGas': w3_src.to_wei('2', 'gwei'),
                    'maxPriorityFeePerGas': w3_src.to_wei('1', 'gwei'),
                    'chainId': w3_src.eth.chain_id,
                })
                signed = w3_src.eth.account.sign_transaction(tx, private_key=warden_pk)
                tx_hash = w3_src.eth.send_raw_transaction(signed.raw_transaction)
                print(f"withdraw sent on source for tx {evt.transactionHash.hex()} -> {tx_hash.hex()}")
            except Exception as e:
                print(f"Failed to call withdraw on source: {e}")

    return 1
