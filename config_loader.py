"""Unified config loader for network, crypto, and profile settings.

This module provides a single source of truth for all configuration,
eliminating hardcoded defaults scattered across entrypoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class NetworkConfig:
    """Network (UDP, transport) configuration."""
    bind_ip: str = "0.0.0.0"
    tx_ip: str = "192.168.0.123"
    tx_port: int = 5000
    rx_port: int = 5000
    mtu_bytes: int = 1500
    max_payload_bytes: int = 1200
    recv_buffer_bytes: int = 8388608
    send_buffer_bytes: int = 8388608
    drop_late_frames: bool = True
    frame_deadline_ms: int = 40
    max_reorder_depth: int = 128
    reassembly_timeout_ms: int = 80
    pacing_enabled: bool = True
    max_burst_packets: int = 4
    inter_packet_gap_us: int = 100


@dataclass
class CryptoConfig:
    """Cryptographic configuration."""
    algorithm: str = "AES-256-GCM"
    tag_bytes: int = 16
    nonce_counter_bits: int = 64
    tx_to_rx_key_id: int = 1
    rx_to_tx_key_id: int = 2
    replay_window_packets: int = 1024
    rekey_interval_seconds: int = 600
    reject_reused_nonce: bool = True
    reject_stale_nonce: bool = True


@dataclass
class SessionConfig:
    """Session-level runtime configuration."""
    network: NetworkConfig
    crypto: CryptoConfig


def load_config(config_dir: Optional[str] = None) -> SessionConfig:
    """Load network.yaml and crypto.yaml from config_dir (defaults to ./config/).
    
    Args:
        config_dir: Path to config directory. If None, uses './config/'.
        
    Returns:
        SessionConfig with network and crypto sub-configs.
        
    Raises:
        FileNotFoundError: If required YAML files are missing.
        yaml.YAMLError: If YAML parsing fails.
    """
    if config_dir is None:
        config_dir = Path(__file__).parent / "config"
    else:
        config_dir = Path(config_dir)

    network_path = config_dir / "network.yaml"
    crypto_path = config_dir / "crypto.yaml"

    if not network_path.exists():
        raise FileNotFoundError(f"network.yaml not found at {network_path}")
    if not crypto_path.exists():
        raise FileNotFoundError(f"crypto.yaml not found at {crypto_path}")

    with open(network_path) as f:
        net_data = yaml.safe_load(f) or {}

    with open(crypto_path) as f:
        crypto_data = yaml.safe_load(f) or {}

    # Flatten network config from YAML structure
    net_dict = {}
    if "udp" in net_data:
        net_dict.update(net_data["udp"])
    if "transport" in net_data:
        net_dict.update({
            "drop_late_frames": net_data["transport"].get("drop_late_frames", True),
            "frame_deadline_ms": net_data["transport"].get("frame_deadline_ms", 40),
            "max_reorder_depth": net_data["transport"].get("max_reorder_depth", 128),
            "reassembly_timeout_ms": net_data["transport"].get("reassembly_timeout_ms", 80),
        })
    if "pacing" in net_data:
        net_dict.update({
            "pacing_enabled": net_data["pacing"].get("enabled", True),
            "max_burst_packets": net_data["pacing"].get("max_burst_packets", 4),
            "inter_packet_gap_us": net_data["pacing"].get("inter_packet_gap_us", 100),
        })

    # Flatten crypto config from YAML structure
    crypto_dict = {}
    if "crypto" in crypto_data:
        crypto_dict.update(crypto_data["crypto"])
    if "keys" in crypto_data:
        crypto_dict.update(crypto_data["keys"])
    if "session" in crypto_data:
        crypto_dict.update(crypto_data["session"])

    network = NetworkConfig(**{k: v for k, v in net_dict.items() if k in NetworkConfig.__dataclass_fields__})
    crypto = CryptoConfig(**{k: v for k, v in crypto_dict.items() if k in CryptoConfig.__dataclass_fields__})

    return SessionConfig(network=network, crypto=crypto)


if __name__ == "__main__":
    config = load_config()
    print(f"Network: {config.network}")
    print(f"Crypto: {config.crypto}")
