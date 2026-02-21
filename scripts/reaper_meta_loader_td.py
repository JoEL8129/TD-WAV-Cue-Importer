"""
TouchDesigner: load Reaper markers/regions from WAV cue chunks into a Table DAT.

Unified table: index | cue_id | position | end | length | label
- index: 1-based time order. cue_id: Reaper/WAV cue ID (creation order).
- Markers (point): position, end=position, length=0, label
- Regions (range): position, end, length=end-position, label

Usage:
  Create COMP with: File (str), Refresh (pulse), meta_out (Table DAT).
  load_meta(parent(), op('meta_out'))
"""

import struct


def _parse_cue_chunk(f, cend):
    """Parse cue chunk; return dict cue_id -> sample_offset."""
    data = {}
    n = struct.unpack("<I", f.read(4))[0]
    for _ in range(n):
        if f.tell() + 24 <= cend:
            i, _, _, _, _, off = struct.unpack("<IIIIII", f.read(24))
            data[i] = off
    return data


def _parse_smpl_chunk(f, cend):
    """Parse smpl chunk; return dict cue_id -> end_sample."""
    data = {}
    f.read(28)
    num_loops = struct.unpack("<I", f.read(4))[0]
    f.read(4)
    for _ in range(num_loops):
        if f.tell() + 24 <= cend:
            loop_id, _, _, end_sample, _, _ = struct.unpack("<IIIIII", f.read(24))
            data[loop_id] = end_sample
    return data


def _parse_labl_chunk(f, csz):
    """Parse standalone labl chunk; return dict cue_id -> name."""
    if csz < 4:
        return {}
    lid = struct.unpack("<I", f.read(4))[0]
    lab = f.read(csz - 4).split(b"\x00")[0].decode("utf-8", errors="replace").strip()
    return {lid: lab}


def _read_wav_meta(path: str) -> list:
    """
    Read WAV cue/labl/smpl/LIST chunks; return [{position, end, length, label}, ...].
    Markers: end=position, length=0. Regions: end>position, length=end-position.
    """
    result = []
    sr = 48000
    cue_data = {}
    labl_data = {}
    ltxt_data = {}
    smpl_data = {}
    with open(path, "rb") as f:
        if f.read(4) != b"RIFF":
            return []
        rsize = struct.unpack("<I", f.read(4))[0]
        if f.read(4) != b"WAVE":
            return []
        end = f.tell() + rsize - 4
        while f.tell() < end:
            cid = f.read(4)
            if len(cid) < 4:
                break
            csz = struct.unpack("<I", f.read(4))[0]
            cend = f.tell() + csz
            if cid == b"fmt " and csz >= 12:
                f.read(4)
                sr = struct.unpack("<I", f.read(4))[0]
                f.seek(cend)
            elif cid == b"cue " and csz >= 4:
                cue_data.update(_parse_cue_chunk(f, cend))
                f.seek(cend)
            elif cid == b"smpl" and csz >= 44:
                smpl_data.update(_parse_smpl_chunk(f, cend))
                f.seek(cend)
            elif cid == b"labl" and csz >= 4:
                for k, v in _parse_labl_chunk(f, csz).items():
                    if k not in labl_data:
                        labl_data[k] = v
                f.seek(cend)
            elif cid == b"LIST" and csz >= 4:
                f.read(4)
                lend = f.tell() + csz - 4
                while f.tell() < lend:
                    sid = f.read(4)
                    if len(sid) < 4:
                        break
                    ssz = struct.unpack("<I", f.read(4))[0]
                    send = f.tell() + ssz
                    if ssz & 1:
                        send += 1
                    if sid in (b"labl", b"note") and ssz >= 4:
                        lid = struct.unpack("<I", f.read(4))[0]
                        lab = f.read(ssz - 4).split(b"\x00")[0].decode("utf-8", errors="replace").strip()
                        if lid not in labl_data or sid == b"labl":
                            labl_data[lid] = lab
                    elif sid == b"ltxt" and ssz >= 20:
                        lid = struct.unpack("<I", f.read(4))[0]
                        sample_len = struct.unpack("<I", f.read(4))[0]
                        f.read(12)
                        txt = f.read(ssz - 20).split(b"\x00")[0].decode("utf-8", errors="replace").strip()
                        ltxt_data[lid] = (sample_len, txt)
                    f.seek(send)
            f.seek(cend)
    if not labl_data and not ltxt_data and not smpl_data:
        return []
    cue_ids_ordered = sorted(cue_data, key=lambda k: cue_data[k])
    orphan_labels = [labl_data[k] for k in labl_data if k not in cue_data]
    orphan_idx = 0
    for cid in cue_ids_ordered:
        pos = cue_data[cid] / sr
        if cid in smpl_data:
            end_pos = smpl_data[cid] / sr
            lab = labl_data.get(cid, "")
            result.append({"cue_id": cid, "position": pos, "end": end_pos, "length": end_pos - pos, "label": lab})
        elif cid in ltxt_data:
            sample_len, ltxt_label = ltxt_data[cid]
            end_pos = pos + (sample_len / sr)
            lab = ltxt_label or labl_data.get(cid, "")
            result.append({"cue_id": cid, "position": pos, "end": end_pos, "length": end_pos - pos, "label": lab})
        else:
            lab = labl_data.get(cid, "")
            if not lab and orphan_idx < len(orphan_labels):
                lab = orphan_labels[orphan_idx]
                orphan_idx += 1
            result.append({"cue_id": cid, "position": pos, "end": pos, "length": 0.0, "label": lab})
    result.sort(key=lambda x: x["position"])
    return result


def load_meta(comp, meta_dat, file_par_name="File") -> None:
    """
    Read markers/regions from comp's File path (WAV); write to Table DAT.
    Columns: index | cue_id | position | end | length | label
    """
    if comp is None or meta_dat is None:
        return
    try:
        file_par = getattr(comp.par, file_par_name, None)
        if file_par is None:
            return
        path = str(file_par.eval()).strip()
        if not path:
            meta_dat.clear()
            meta_dat.appendRow(["index", "cue_id", "position", "end", "length", "label"])
            return
        meta = _read_wav_meta(path)
    except Exception as e:
        meta_dat.clear()
        meta_dat.appendRow(["error", str(e)])
        return

    meta_dat.clear()
    meta_dat.appendRow(["index", "cue_id", "position", "end", "length", "label"])
    for i, m in enumerate(meta, 1):
        meta_dat.appendRow([
            str(i),
            str(m.get("cue_id", "")),
            f"{m['position']:.3f}",
            f"{m['end']:.3f}",
            f"{m['length']:.3f}",
            m.get("label", ""),
        ])
