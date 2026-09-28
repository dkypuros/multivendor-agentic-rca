"""
MRF: Media Resource Function — the IMS media plane (MRFC control + MRFP media processing).

The MRF is the network function that OWNS media: it hosts conference bridges, plays announcements,
and transcodes codecs on behalf of the IMS. In 3GPP TS 23.228 it splits into two logical halves —
the MRFC (Media Resource Function Controller) handles the control signalling (SIP/SDP, the conference
state machine, announcement triggering), and the MRFP (Media Resource Function Processor) handles the
bearer (RTP termination, audio mixing, transcoding). A real deployment separates them over H.248/MEGACO;
this owned MRF combines both behind ONE media-resource control surface, exactly as the reference does,
and is honest that the MRFP half is MODELLED — no RTP is bound, no codec is run, no DSP exists.

What this NF actually does: it is a RESOURCE CONTROLLER. It allocates a media resource for a
conference / announcement / transcoding request out of a finite MRFP pool, tracks participants and
played announcements against it, and releases the resource on teardown. When the pool is full it
answers an HONEST 503 rather than pretending it has capacity it does not. The media itself — the
mixed audio, the played prompt, the transcoded stream — is a labelled drawing, not a running bearer.

This is a STANDALONE build. No other NF is edited. The S-CSCF-driven invocation of the MRF (the
S-CSCF routing an INVITE to a conference-factory URI, or triggering an announcement) is the
IMS-integration follow-up; here the MRF's media-resource surface is proven on its own.

Spec anchors:
  MRFC functional description        TS 23.228 section 4.2.5 — media resource control (SIP/SDP,
                                     conference state, announcement triggering). Modelled by the
                                     control surface below.
  MRFP functional description        TS 23.228 section 4.2.6 — RTP termination, mixing, transcoding.
                                     MODELLED: a labelled port pool, no real bearer (see ledger).
  Media resource control             TS 23.228 section 4.7 — allocation/release of media resources.
  Conferencing using IMS             TS 24.147 — conference factory, join/leave, member control.
  Announcement / tones               TS 23.218 section 7 — play an announcement to a session.
  Nnrf_NFManagement (register)       TS 29.510 5.2 — the MRF registers in the NRF so the IMS core
                                     can discover the media plane. (MRF is an IMS/SIP element, not a
                                     native SBI NF; NRF registration is a lab convenience for
                                     discovery, labelled below.)

Labeled simplifications (ledgered in procedures/mrf_media.txt):
  - NO real RTP / codecs / DSP. Allocated rtp/rtcp ports and codec strings are MODELLED labels off a
    finite pool; nothing is bound and no audio is mixed, played, or transcoded. The MRFP is a drawing.
  - Media resource control is HTTP/JSON, not SIP/SDP + H.248. The resource shapes (conference,
    participant, announcement, transcoding) are preserved so the learning transfers; the wire is not.
  - S-CSCF-driven invocation (INVITE to a conference-factory URI / announcement trigger) is NOT here.
    The MRF is proven standalone; IMS integration is the follow-up.
  - NRF registration models MRF discovery; a real MRF is reached over SIP at its media-control URI.

Run: python3 mrf.py   (SBI on 127.0.0.1:7037, registers with the NRF as MRF)
"""

import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from domain import netconfig, obs
from domain.netconfig import port, url
from domain.statestore import open_store

PORT = port("mrf")
NRF = url("nrf")
app = SbiApp("mrf")

# Persistence mirrors the other NFs: sqlite3 when TELCO_STATE_DIR is set, in-memory otherwise
# (domain/statestore.py). Media sessions are keyed by mediaSessionId.
store = open_store("mrf")
media_sessions = store.collection("media_sessions")

# --- MRFP media configuration (MODELLED — TS 23.228 4.2.6). ---------------------------------------
# The MRFP terminates RTP; here its capacity is a finite pool of RTP/RTCP port PAIRS, allocated on
# resource request and released on teardown. NOTHING is bound to these numbers — they are labels an
# SDP answer would carry. Pool size is configurable (TELCO_MRF_POOL_PAIRS) so exhaustion — the honest
# 503 when the media plane is full — is provable in a spec.
RTP_ADDRESS = netconfig.value("TELCO_MRF_RTP_ADDRESS", "127.0.0.1")
RTP_BASE = int(netconfig.value("TELCO_MRF_RTP_BASE", "40000"))     # modelled RTP port base (a label)
POOL_PAIRS = int(netconfig.value("TELCO_MRF_POOL_PAIRS", "128"))   # MRFP capacity, in port pairs

# Supported codecs the MRFP would transcode between (TS 26.114 / RFC 3551 payloads). A media session
# asking for an unsupported codec is rejected 400 rather than silently downgraded.
SUPPORTED_CODECS = ["PCMU/8000", "PCMA/8000", "G729/8000", "AMR/8000", "AMR-WB/16000"]
DEFAULT_CODEC = "PCMU/8000"
RESOURCE_TYPES = {"conference", "announcement", "transcoding"}
DEFAULT_MAX_PARTICIPANTS = 32


class MediaResourcePool:
    """The MRFP's finite media capacity, modelled as a pool of RTP/RTCP port pairs (TS 23.228 4.2.6).
    allocate() returns a modelled endpoint or None when exhausted — the caller turns None into an
    honest 503. This is the whole point of the MRF being a RESOURCE CONTROLLER: capacity is real
    even though the bearer is a drawing."""

    def __init__(self, base, pairs):
        self._free = [(base + 2 * i, base + 2 * i + 1) for i in range(pairs)]
        self._capacity = pairs

    def allocate(self):
        if not self._free:
            return None
        rtp, rtcp = self._free.pop(0)
        return {"address": RTP_ADDRESS, "rtpPort": rtp, "rtcpPort": rtcp, "modelled": True}

    def release(self, endpoint):
        if not endpoint:
            return
        pair = (endpoint["rtpPort"], endpoint["rtcpPort"])
        if pair not in self._free:
            self._free.append(pair)
            self._free.sort()

    def available(self):
        return len(self._free)

    def capacity(self):
        return self._capacity


pool = MediaResourcePool(RTP_BASE, POOL_PAIRS)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _get_session(session_id):
    return media_sessions.get(session_id)


# ------------------------------------------------------ Media resource allocation (MRFC/MRFP)
# TS 23.228 4.7: allocate a media resource for a conference / announcement / transcoding request.
# The MRFC accepts the control request; the MRFP hands back a media endpoint (MODELLED port pair).

@app.route("POST", "/mrf/v1/media-sessions")
def allocate_media_session(params, query, body):
    resource_type = body.get("resourceType", "conference")
    if resource_type not in RESOURCE_TYPES:
        return problem(400, "Bad Request",
                       detail=f"resourceType must be one of {sorted(RESOURCE_TYPES)}",
                       cause="UNSUPPORTED_RESOURCE_TYPE")
    codec = body.get("codec", DEFAULT_CODEC)
    if codec not in SUPPORTED_CODECS:
        return problem(400, "Bad Request",
                       detail=f"codec {codec} not in {SUPPORTED_CODECS}",
                       cause="UNSUPPORTED_CODEC")
    # For transcoding, both endpoints' codecs must be supported (RFC 3264 offer/answer, modelled).
    output_codec = body.get("outputCodec")
    if resource_type == "transcoding":
        output_codec = output_codec or DEFAULT_CODEC
        if output_codec not in SUPPORTED_CODECS:
            return problem(400, "Bad Request",
                           detail=f"outputCodec {output_codec} not in {SUPPORTED_CODECS}",
                           cause="UNSUPPORTED_CODEC")

    # MRFP allocation — the honest capacity gate. Pool empty => 503, not a faked resource.
    endpoint = pool.allocate()
    if endpoint is None:
        obs.counter("mrf_allocations_rejected_total").inc()
        obs.log("media_resource_exhausted", resourceType=resource_type,
                capacity=pool.capacity(), available=0)
        return problem(503, "Service Unavailable",
                       detail="MRFP media resource pool is full — no port pair available",
                       cause="INSUFFICIENT_MEDIA_RESOURCES")

    session_id = uuid.uuid4().hex
    record = {
        "mediaSessionId": session_id,
        "resourceType": resource_type,
        "state": "ACTIVE",
        "codec": codec,
        "name": body.get("name", resource_type),
        # The conference-factory URI an S-CSCF would route an INVITE to (TS 24.147). Modelled.
        "resourceUri": f"sip:{resource_type}-{session_id[:8]}@mrf.ims.owned",
        # The MRFP anchor/mixer endpoint (MODELLED — no RTP bound).
        "mediaEndpoint": endpoint,
        "maxParticipants": int(body.get("maxParticipants", DEFAULT_MAX_PARTICIPANTS)),
        "participants": [],
        "announcements": [],
        "self": f"/mrf/v1/media-sessions/{session_id}",
        "createdAt": now_iso(),
        # Honesty marker on the resource itself: control is real, the bearer is a drawing.
        "mediaPlane": "MODELLED_NO_RTP",
    }
    if resource_type == "transcoding":
        record["inputCodec"] = codec
        record["outputCodec"] = output_codec
    media_sessions.put(session_id, record)
    obs.log("media_session_allocated", mediaSessionId=session_id, resourceType=resource_type,
            codec=codec, rtpPort=endpoint["rtpPort"], available=pool.available())
    obs.counter("mrf_media_sessions_total").inc()
    return 201, record


@app.route("GET", "/mrf/v1/media-sessions/{mediaSessionId}")
def get_media_session(params, query, body):
    record = _get_session(params["mediaSessionId"])
    if record is None:
        return problem(404, "Not Found", detail="no such media session",
                       cause="MEDIA_SESSION_NOT_FOUND")
    return 200, record


@app.route("GET", "/mrf/v1/media-sessions")
def list_media_sessions(params, query, body):
    sessions = list(media_sessions.values())
    return 200, {"mediaSessions": sessions, "count": len(sessions),
                 "poolCapacity": pool.capacity(), "poolAvailable": pool.available()}


# --------------------------------------------------------- Add a participant to a conference
# TS 24.147: a party joins a conference. The MRFC creates a member; the MRFP allocates that member's
# media endpoint (MODELLED port pair). Pool empty => honest 503. Conference full => 503.

@app.route("POST", "/mrf/v1/media-sessions/{mediaSessionId}/participants")
def add_participant(params, query, body):
    record = _get_session(params["mediaSessionId"])
    if record is None:
        return problem(404, "Not Found", detail="no such media session",
                       cause="MEDIA_SESSION_NOT_FOUND")
    if record["resourceType"] != "conference":
        return problem(409, "Conflict",
                       detail="participants can only join a conference media session",
                       cause="NOT_A_CONFERENCE")
    if record["state"] != "ACTIVE":
        return problem(409, "Conflict", detail="media session is not active",
                       cause="MEDIA_SESSION_NOT_ACTIVE")
    if len(record["participants"]) >= record["maxParticipants"]:
        return problem(503, "Service Unavailable", detail="conference is full",
                       cause="CONFERENCE_FULL")

    endpoint = pool.allocate()
    if endpoint is None:
        obs.counter("mrf_allocations_rejected_total").inc()
        obs.log("media_resource_exhausted", mediaSessionId=record["mediaSessionId"],
                capacity=pool.capacity(), available=0)
        return problem(503, "Service Unavailable",
                       detail="MRFP media resource pool is full — cannot admit participant",
                       cause="INSUFFICIENT_MEDIA_RESOURCES")

    participant = {
        "participantId": uuid.uuid4().hex,
        "uri": body.get("uri", f"sip:party-{uuid.uuid4().hex[:6]}@ims.owned"),
        "displayName": body.get("displayName", ""),
        "isModerator": bool(body.get("isModerator", False)),
        "state": "CONNECTED",
        "mediaEndpoint": endpoint,          # MODELLED — no RTP bound
        "codec": record["codec"],
        "joinedAt": now_iso(),
    }
    record["participants"].append(participant)
    media_sessions.put(record["mediaSessionId"], record)
    obs.log("participant_joined", mediaSessionId=record["mediaSessionId"],
            participantId=participant["participantId"], uri=participant["uri"],
            participantCount=len(record["participants"]))
    obs.counter("mrf_participants_total").inc()
    return 201, {"participant": participant, "participantCount": len(record["participants"]),
                 "mediaSessionId": record["mediaSessionId"]}


# ------------------------------------------------------------------ Play an announcement
# TS 23.218 section 7: play an announcement / tone to a media session. MODELLED — the playback is
# recorded and reported PLAYING, but no audio file is streamed and no TTS is synthesised.

@app.route("POST", "/mrf/v1/media-sessions/{mediaSessionId}/play")
def play_announcement(params, query, body):
    record = _get_session(params["mediaSessionId"])
    if record is None:
        return problem(404, "Not Found", detail="no such media session",
                       cause="MEDIA_SESSION_NOT_FOUND")
    if record["state"] != "ACTIVE":
        return problem(409, "Conflict", detail="media session is not active",
                       cause="MEDIA_SESSION_NOT_ACTIVE")
    playback = {
        "playbackId": uuid.uuid4().hex,
        "announcement": body.get("announcement", "welcome"),
        "ttsText": body.get("ttsText"),
        "language": body.get("language", "en"),
        "loop": bool(body.get("loop", False)),
        "state": "PLAYING",                 # MODELLED — no bearer streamed
        "startedAt": now_iso(),
        "mediaPlane": "MODELLED_NO_AUDIO",
    }
    record["announcements"].append(playback)
    media_sessions.put(record["mediaSessionId"], record)
    obs.log("announcement_played", mediaSessionId=record["mediaSessionId"],
            playbackId=playback["playbackId"], announcement=playback["announcement"])
    obs.counter("mrf_announcements_total").inc()
    return 200, {"playback": playback, "mediaSessionId": record["mediaSessionId"]}


# --------------------------------------------------------------------- Release a media session
# TS 23.228 4.7: release the media resource. Every MODELLED port pair — the anchor and every
# participant's — is returned to the MRFP pool, so a subsequent allocation can succeed.

@app.route("DELETE", "/mrf/v1/media-sessions/{mediaSessionId}")
def release_media_session(params, query, body):
    record = _get_session(params["mediaSessionId"])
    if record is None:
        return problem(404, "Not Found", detail="no such media session",
                       cause="MEDIA_SESSION_NOT_FOUND")
    pool.release(record.get("mediaEndpoint"))
    for participant in record.get("participants", []):
        pool.release(participant.get("mediaEndpoint"))
    media_sessions.delete(record["mediaSessionId"])
    obs.log("media_session_released", mediaSessionId=record["mediaSessionId"],
            resourceType=record["resourceType"],
            freed_pairs=1 + len(record.get("participants", [])), available=pool.available())
    obs.counter("mrf_media_sessions_released_total").inc()
    return 204, {}


def register_with_nrf():
    # NFProfile per TS 29.510 6.1.6.2.2. The MRF is an IMS/SIP media element, not a native SBI NF;
    # registering it in the NRF is a lab convenience so the IMS core (and the viewer) can DISCOVER
    # the media plane the same way every other function is discovered (labelled in the docstring).
    profile = {"nfType": "MRF", "nfStatus": "REGISTERED", "ipv4Addresses": ["127.0.0.1"],
               "nfServices": [
                   {"serviceName": "nmrf-mediaresource",
                    "ipEndPoints": [{"ipv4Address": "127.0.0.1", "port": PORT}]}]}
    for _ in range(25):
        try:
            request("PUT", f"{NRF}/nnrf-nfm/v1/nf-instances/{uuid.uuid4()}", profile)
            return
        except OSError:
            time.sleep(0.2)


def _scrape():
    sessions = list(media_sessions.values())
    obs.gauge("mrf_media_sessions_active").set(len(sessions))
    obs.gauge("mrf_participants_active").set(sum(len(s.get("participants", [])) for s in sessions))
    obs.gauge("mrf_media_pool_available").set(pool.available())
    obs.gauge("mrf_media_pool_capacity").set(pool.capacity())


if __name__ == "__main__":
    obs.init("mrf")
    obs.on_scrape(_scrape)
    register_with_nrf()
    serve(app, PORT)
