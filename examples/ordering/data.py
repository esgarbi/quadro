"""
Static data for the ordering system example.

Product catalog curated from ElectronicsProductsPricingData.csv,
supplier directory with simulated suppliers, initial warehouse
stock (some deliberately low to trigger procurement), and a queue of
customer orders with diverse names and addresses that the run loop posts progressively.
"""

from __future__ import annotations

# ── Product catalog ────────────────────────────────────────────────────────────
# 25 electronics products with realistic pricing and categories.  SKU is a short
# stable id derived from the CSV row; price is the rounded average of min/max.

PRODUCT_CATALOG: dict[str, dict] = {
    "SKU-TV-SANUS": {
        "name": "Sanus VLF410 Super Slim Full-Motion TV Mount",
        "brand": "Sanus",
        "price": 104.99,
        "category": "TV Accessories",
    },
    "SKU-SPK-BOYTONE": {
        "name": "Boytone BT-210F 2.1-Ch Home Theater System",
        "brand": "Boytone",
        "price": 69.99,
        "category": "Home Audio",
    },
    "SKU-CAM-RING": {
        "name": "Ring Video Doorbell Pro 2",
        "brand": "Ring",
        "price": 249.99,
        "category": "Smart Home",
    },
    "SKU-HPH-SONY": {
        "name": "Sony WH-1000XM5 Wireless Noise-Canceling Headphones",
        "brand": "Sony",
        "price": 398.00,
        "category": "Headphones",
    },
    "SKU-TAB-SAMSUNG": {
        "name": "Samsung Galaxy Tab S9 Ultra 256GB",
        "brand": "Samsung",
        "price": 1199.99,
        "category": "Tablets",
    },
    "SKU-KBD-LOGITECH": {
        "name": "Logitech MX Keys Advanced Wireless Keyboard",
        "brand": "Logitech",
        "price": 119.99,
        "category": "Computer Accessories",
    },
    "SKU-MON-LG": {
        "name": "LG UltraGear 27-Inch QHD Gaming Monitor",
        "brand": "LG",
        "price": 349.99,
        "category": "Monitors",
    },
    "SKU-RTR-NETGEAR": {
        "name": "Netgear Nighthawk AX12 WiFi 6 Router",
        "brand": "Netgear",
        "price": 399.99,
        "category": "Networking",
    },
    "SKU-SSD-SAMSUNG": {
        "name": "Samsung 990 PRO 2TB NVMe SSD",
        "brand": "Samsung",
        "price": 169.99,
        "category": "Storage",
    },
    "SKU-CHR-GOOGLE": {
        "name": "Google Chromecast with Google TV (4K)",
        "brand": "Google",
        "price": 49.99,
        "category": "Streaming Devices",
    },
    "SKU-SPK-JBL": {
        "name": "JBL Charge 5 Portable Bluetooth Speaker",
        "brand": "JBL",
        "price": 179.95,
        "category": "Portable Audio",
    },
    "SKU-CAM-GOPRO": {
        "name": "GoPro HERO12 Black Action Camera",
        "brand": "GoPro",
        "price": 399.99,
        "category": "Cameras",
    },
    "SKU-MOU-RAZER": {
        "name": "Razer DeathAdder V3 Pro Gaming Mouse",
        "brand": "Razer",
        "price": 149.99,
        "category": "Gaming Peripherals",
    },
    "SKU-PWR-ANKER": {
        "name": "Anker Prime 27,650mAh Power Bank (250W)",
        "brand": "Anker",
        "price": 179.99,
        "category": "Power & Charging",
    },
    "SKU-EAR-APPLE": {
        "name": "Apple AirPods Pro (2nd Generation, USB-C)",
        "brand": "Apple",
        "price": 249.00,
        "category": "Earbuds",
    },
    "SKU-WCH-GARMIN": {
        "name": "Garmin Fēnix 7X Sapphire Solar",
        "brand": "Garmin",
        "price": 899.99,
        "category": "Wearables",
    },
    "SKU-HUB-UGREEN": {
        "name": "UGREEN Revodok Pro 13-in-1 USB-C Docking Station",
        "brand": "UGREEN",
        "price": 129.99,
        "category": "Computer Accessories",
    },
    "SKU-PRJ-EPSON": {
        "name": "Epson Home Cinema 3800 4K PRO-UHD Projector",
        "brand": "Epson",
        "price": 1699.99,
        "category": "Projectors",
    },
    "SKU-STR-ROKU": {
        "name": "Roku Ultra 4K/HDR/Dolby Vision Streaming Device",
        "brand": "Roku",
        "price": 99.99,
        "category": "Streaming Devices",
    },
    "SKU-DRN-DJI": {
        "name": "DJI Mini 4 Pro Drone with RC 2 Controller",
        "brand": "DJI",
        "price": 959.00,
        "category": "Drones",
    },
    "SKU-VR-META": {
        "name": "Meta Quest 3 128GB Mixed Reality Headset",
        "brand": "Meta",
        "price": 499.99,
        "category": "Virtual Reality",
    },
    "SKU-MIC-SHURE": {
        "name": "Shure SM7B Vocal Dynamic Microphone",
        "brand": "Shure",
        "price": 399.00,
        "category": "Pro Audio",
    },
    "SKU-GPU-NVIDIA": {
        "name": "NVIDIA GeForce RTX 4090 Founders Edition",
        "brand": "NVIDIA",
        "price": 1599.00,
        "category": "PC Components",
    },
    "SKU-STL-STARLINK": {
        "name": "Starlink Standard Kit (Roam/Residential)",
        "brand": "SpaceX",
        "price": 599.00,
        "category": "Networking",
    },
    "SKU-EBK-RAD": {
        "name": "Rad Power Bikes RadRunner 3 Plus Utility E-Bike",
        "brand": "Rad Power Bikes",
        "price": 2099.00,
        "category": "E-Bikes",
    },
}


# ── Supplier directory ─────────────────────────────────────────────────────────
# Expanded suppliers with different price/lead-time trade-offs and locations.
# price_multiplier is applied to the catalog price to get the wholesale cost.

SUPPLIERS: list[dict] = [
    {
        "name": "FastShip Electronics",
        "location": "Dallas, TX, USA",
        "price_multiplier": 0.85,
        "lead_time_days": 1,
        "reliability": "high",
        "min_order": 5,
    },
    {
        "name": "BulkDeal Distributors",
        "location": "Rotterdam, Netherlands",
        "price_multiplier": 0.65,
        "lead_time_days": 6,
        "reliability": "medium",
        "min_order": 20,
    },
    {
        "name": "ValueSource Global",
        "location": "Mumbai, India",
        "price_multiplier": 0.72,
        "lead_time_days": 4,
        "reliability": "high",
        "min_order": 10,
    },
    {
        "name": "Shenzhen Direct Components",
        "location": "Shenzhen, China",
        "price_multiplier": 0.55,
        "lead_time_days": 14,
        "reliability": "medium",
        "min_order": 50,
    },
    {
        "name": "Premium AV Wholesale",
        "location": "Frankfurt, Germany",
        "price_multiplier": 0.78,
        "lead_time_days": 3,
        "reliability": "very high",
        "min_order": 2,
    },
]


# ── Initial warehouse stock ────────────────────────────────────────────────────
# Some SKUs are deliberately low to trigger the procurement path.

INITIAL_WAREHOUSE: dict[str, int] = {
    "SKU-TV-SANUS": 25,
    "SKU-SPK-BOYTONE": 40,
    "SKU-CAM-RING": 3,  # low — will trigger procurement
    "SKU-HPH-SONY": 15,
    "SKU-TAB-SAMSUNG": 2,  # low — will trigger procurement
    "SKU-KBD-LOGITECH": 50,
    "SKU-MON-LG": 8,
    "SKU-RTR-NETGEAR": 12,
    "SKU-SSD-SAMSUNG": 30,
    "SKU-CHR-GOOGLE": 100,
    "SKU-SPK-JBL": 20,
    "SKU-CAM-GOPRO": 1,  # very low — will trigger procurement
    "SKU-MOU-RAZER": 45,
    "SKU-PWR-ANKER": 60,
    "SKU-EAR-APPLE": 10,
    "SKU-WCH-GARMIN": 18,
    "SKU-HUB-UGREEN": 75,
    "SKU-PRJ-EPSON": 4,
    "SKU-STR-ROKU": 55,
    "SKU-DRN-DJI": 0,  # zero — will definitely trigger procurement
    "SKU-VR-META": 22,
    "SKU-MIC-SHURE": 14,
    "SKU-GPU-NVIDIA": 1,  # very low — will trigger procurement
    "SKU-STL-STARLINK": 5,
    "SKU-EBK-RAD": 3,  # low — will trigger procurement
}

CUSTOMER_DETAILS: list[dict] = [
    {
        "name": "John Doe",
        "address": "903 Winter Palace Drive, Penthouse C, Chicago, IL 60611",
    },
    {
        "name": "Carlos Garcia",
        "address": "102 Surfside Boulevard, Honolulu, HI 96815",
    },
    {
        "name": "Maria Harisso",
        "address": "102 Surfside Boulevard, Honolulu, HI 96815",
    },
    {
        "name": "Peter Smith",
        "address": "102 Surfside Boulevard, Honolulu, HI 96815",
    },
    {
        "name": "Erick Johnson",
        "address": "102 Surfside Boulevard, Honolulu, HI 96815",
    },
    {
        "name": "Liam Wilson",
        "address": "102 Surfside Boulevard, Honolulu, HI 96815",
    },
    {
        "name": "Oliver Brown",
        "address": "102 Surfside Boulevard, Honolulu, HI 96815",
    },
    {
        "name": "James Davis",
        "address": "102 Surfside Boulevard, Honolulu, HI 96815",
    },
    {
        "name": "Fernando Pereira",
        "address": "102 Surfside Boulevard, Honolulu, HI 96815",
    },
    {
        "name": "Gabriel Silva",
        "address": "102 Surfside Boulevard, Honolulu, HI 96815",
    },
    {
        "name": "Hugo Costa",
        "address": "102 Surfside Boulevard, Honolulu, HI 96815",
    },
    {
        "name": "Isaac Almeida",
        "address": "102 Surfside Boulevard, Honolulu, HI 96815",
    },
    {
        "name": "Lucas Oliveira",
        "address": "102 Surfside Boulevard, Honolulu, HI 96815",
    },
]

# ── Shipping carriers ─────────────────────────────────────────────────────────

CARRIERS: list[dict] = [
    {
        "name": "FedEx Express",
        "base_cost": 12.99,
        "speed_days": 2,
        "type": "Domestic Express",
    },
    {
        "name": "UPS Ground",
        "base_cost": 8.49,
        "speed_days": 5,
        "type": "Domestic Economy",
    },
    {
        "name": "DHL Express Worldwide",
        "base_cost": 24.99,
        "speed_days": 3,
        "type": "International Fast",
    },
    {
        "name": "USPS Priority Mail",
        "base_cost": 7.99,
        "speed_days": 3,
        "type": "Domestic Standard",
    },
    {
        "name": "OnTrac Regional",
        "base_cost": 6.50,
        "speed_days": 1,
        "type": "Regional Next-Day",
    },
]
