from modules.redis_client import get_redis
from modules.orphan_position_checker import check_orphaned_positions
from quart import Blueprint, jsonify, request
import json

admin_tools = Blueprint("admin_tools", __name__)


@admin_tools.route("/debug/redis-keys", methods=["GET"])
async def list_redis_keys():
    r = get_redis()
    keys = await r.keys("position_state:*")
    return jsonify({
        "total_keys": len(keys),
        "keys": keys
    }), 200


@admin_tools.route("/debug/redis-reversal-keys", methods=["GET"])
async def list_redis_keys():
    r = get_redis()
    keys = await r.keys("reverse_loss:*")
    return jsonify({
        "total_keys": len(keys),
        "keys": keys
    }), 200


@admin_tools.route("/debug/cleanup-position-true", methods=["POST"])
async def cleanup_invalid_position_ids():
    r = get_redis()
    keys = await r.keys("position_state:*")
    deleted = []

    for key in keys:
        try:
            raw = await r.get(key)
            if not raw:
                continue

            value = json.loads(raw)
            if value.get("position_id") is True:
                await r.delete(key)
                deleted.append(key)

        except Exception as e:
            print(f"[CLEANUP ERROR] {key}: {e}")

    return jsonify({
        "status": "completed",
        "keys_deleted": deleted,
        "total_deleted": len(deleted)
    }), 200


@admin_tools.route("/debug/redis-state/<path:key>", methods=["GET"])
async def get_key_state(key):
    try:
        r = get_redis()
        value = await r.get(key)
        if value is None:
            return jsonify({"error": "Key not found"}), 404
        return jsonify({"key": key, "value": json.loads(value)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_tools.route("/debug/redis-delete/<path:key>", methods=["DELETE"])
async def delete_key(key):
    try:
        r = get_redis()
        deleted = await r.delete(key)
        if deleted == 1:
            return jsonify({"status": "deleted", "key": key}), 200
        else:
            return jsonify({"status": "not found", "key": key}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_tools.route("/position_recon", methods=["GET"])
async def run_orphan_check():
    """Sync check to run orphan recovery manually from CLI or admin UI"""
    await check_orphaned_positions()
