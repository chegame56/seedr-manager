[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/your-username/seedr-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/your-username/seedr-manager/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/seedr-manager.svg)](https://pypi.org/project/seedr-manager)
[![Windows Build](https://github.com/your-username/seedr-manager/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/your-username/seedr-manager/actions/workflows/ci.yml)

# Seedr Manager

Automate uploading torrents to Seedr, strip `private` tags, generate magnet links with 1400+ public trackers, and download via IDM.

## 🚀 Features

- **Seedr Upload** – Push local `.torrent` files to your Seedr account.  
- **Private Flag Removal** – Removes BitTorrent `private` flag and injects known public trackers (≥1400).  
- **Magnet Conversion** – Builds robust magnet links for maximum DHT availability.  
- **IDM Launcher** – Spawns Internet Download Manager for seamless downloading.

## 📦 Installation

```bash


# from source
git clone https://github.com/your-username/seedr-manager.git
cd seedr-manager
pip install .
