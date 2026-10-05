# Home Lab Infrastructure Specification

Detailed layout of the home lab compute, networking, and persistent storage infrastructure.

## Network Architecture

- **Gateway / Firewall:** Protectli Vault FW4C running OPNsense.
  - WAN: Dual 1Gbps fiber connections with gateway failover and traffic shaping (FQ-CoDel).
  - LAN: 10GbE SFP+ DAC uplink to core switch.
- **Core Switch:** Mikrotik CRS309-1G-8S+IN (8x 10GbE SFP+ ports).
- **Distribution Switch:** UniFi USW-24-PoE for cameras, access points, and IoT gateways.
- **VLAN Segmentation:**
  - VLAN 10 (Management): IPMI, switch management, hypervisor consoles.
  - VLAN 20 (Trusted LAN): Workstations, primary laptops, mobile devices.
  - VLAN 30 (Servers / Services): Container hosts, NAS interfaces, reverse proxies.
  - VLAN 40 (IoT / Isolated): Environmental sensors, smart plugs, cameras (no WAN ingress/egress).
  - VLAN 50 (Guest): Rate-limited visitor network with client isolation.

## Compute Clusters

### Hypervisor Nodes (Proxmox VE 8.x)
- **Node 1 (Primary Workloads):**
  - Chassis: Minisforum MS-01
  - CPU: Intel Core i9-13900H (14 cores, 20 threads)
  - RAM: 64GB DDR5 ECC SO-DIMM
  - Storage: 2x 2TB Kingston KC3000 PCIe 4.0 NVMe (ZFS mirror for VM root)
  - Networking: Dual 10G SFP+ bonded in LACP
- **Node 2 (Development & Staging):**
  - Chassis: Minisforum MS-01
  - CPU: Intel Core i9-13900H
  - RAM: 64GB DDR5
  - Storage: 2x 2TB NVMe ZFS mirror

## Storage Infrastructure

- **Storage Appliance:** TrueNAS SCALE on customized Supermicro 2U 12-bay chassis.
- **Pool Layout:**
  - `tank-fast`: 4x 1.92TB enterprise SATA SSDs (RAIDZ1) for VM disk backing via iSCSI.
  - `tank-mass`: 6x 18TB Seagate Exos Enterprise HDDs (RAIDZ2) for backups, media, and archive storage.
- **Access Protocols:** NFSv4 for Linux container volume mounts; SMB3 with multichannel enabled for workstations.

## Backup & Disaster Recovery (3-2-1 Strategy)

1. **Local Snapshots:** ZFS automated hourly snapshots kept for 48 hours, daily snapshots kept for 30 days.
2. **On-Site Secondary:** Nightly replication of critical ZFS datasets to a standalone TrueNAS backup box in the garage.
3. **Off-Site Cold Copy:** Weekly encrypted BorgBackup archives pushed to Backblaze B2 object storage using client-side AES-256 encryption.
