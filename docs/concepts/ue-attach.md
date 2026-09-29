# The UE attach

[Glossary](../glossary.md#ue-and-attach) · [All concepts](README.md)

## What it is

Before a phone (a **UE**) can send data on a 5G network it has to:

1. **Connect to the radio** (RRC setup).
2. **Register** with the core (NAS registration), which includes **5G-AKA**: the network and the UE each prove they know the same secret key without sending it. The math is **MILENAGE**, a 3GPP-standard algorithm set built on AES.
3. **Open a PDU session**: the core assigns an IP address and sets up a user-plane tunnel (**GTP-U**) from the RAN to the UPF.

## In this demo

When Start finds the cell `ACTIVE`, the console runs `ue_sim` once. It walks all three stages through the real RAN and core in the slice, then sends 3 echoes through the tunnel. From the example run:

```text
UE -> gNB RRCSetupRequest              gNB -> UE RRCSetup
UE -> gNB RegistrationRequest          gNB -> UE AuthenticationRequest
udm:  auth_vector_generated  supi=imsi-001010000000001
ausf: authentication_result  authResult=AUTHENTICATION_SUCCESS
UE -> gNB AuthenticationResponse       gNB -> UE SecurityModeCommand
eir:  equipment_checked      status=WHITELISTED
nssf: ns_selection           allowed=['1']
amf:  registration_accepted  state=REGISTERED
UE -> gNB PduSessionEstablishmentRequest  gNB -> UE PduSessionEstablishmentAccept
pcf:  sm_policy_created      policy=internet-default fiveqi=9
upf:  n4_session_established
chf:  charging_data_created
smf:  session_created        ueIp=10.45.0.3
[ue_sim] ECHO 3/3 replies via the RAN over the GTP-U tunnel
```

The UE and the UDM compute the MILENAGE values **independently**; registration only succeeds if they match. The console's UE panel shows `REGISTERED`, the PDU address and `3/3`.

## Honest limits

- The radio is simulated: RRC and NAS messages travel as JSON between processes, not over the air.
- One subscriber, provisioned in the UDM's data file. The key is a published 3GPP test key.
- The UE doesn't model radio-link failure, so Inject doesn't produce "UE lost signal" events; the fault shows up at the timing and DU layers.
- The attach runs once per Start. It isn't repeated during Inject or RCA.

## Code

- UE simulator: [ue_sim.py#L75](../../src/services/ue_sim/ue_sim.py#L75) (registration), [#L115](../../src/services/ue_sim/ue_sim.py#L115) (PDU session and echo)
- MILENAGE and key derivation: [milenage.py](../../src/adapters/milenage.py)
- Subscriber data: [subscribers.json](../../src/services/core/udm/subscribers.json)
- Core functions: [amf/](../../src/services/core/amf), [ausf/](../../src/services/core/ausf), [udm/](../../src/services/core/udm), [smf/](../../src/services/core/smf), [upf/](../../src/services/core/upf)
- Console trigger: [sandbox_controller.py#L194](../../src/services/ran/sandbox_controller.py#L194)
