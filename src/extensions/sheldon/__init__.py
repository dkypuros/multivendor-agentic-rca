"""extensions/sheldon — federation of the REAL Sheldon bare-metal O-Cloud.

Sheldon runs a right-sized Redfish/Metal3 O-Cloud (sushy-emulator + libvirt VMs presented
as Redfish BMCs, driven by Metal3/Ironic; see docs/ocloud/sheldon_ocloud.md). This package welds
that REAL infrastructure inventory into the owned stack's O2-IMS (services/edge/ocloud.py), the
same read-only federation discipline extensions/duranta uses for the real OAI 5G core.

  metal3.py            the only place that touches the live Sheldon cluster (read-only)
  o2ims_federation.py  maps real BareMetalHosts -> O2-IMS resources (ocloud.py shape)
  weld.py              registers the real nodes INTO the owned stack's O2-IMS (reversible)
"""
