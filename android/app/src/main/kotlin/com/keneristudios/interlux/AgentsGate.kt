package com.keneristudios.interlux

import android.content.Context
import android.os.Handler
import android.os.Looper
import com.keneristudios.interlux.agent.AgentAuth
import com.keneristudios.interlux.agent.AgentWs
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import org.json.JSONObject

/**
 * Agents screen bridge (step 1b): `interlux/agents` channel.
 *
 * Same calls the AgentControl pairing intents make, minus the
 * PendingIntent round trip — this process talking to its own daemon.
 * Methods: pairingList, pairingApprove {id}, pairingDeny {id},
 * pairingRevoke {agentId}. Results are plain maps (pending[], agents[])
 * or {error}; a null result means pairing is required first.
 */
class AgentsGate(
    messenger: BinaryMessenger,
    private val context: Context,
) : MethodChannel.MethodCallHandler {

    private val channel = MethodChannel(messenger, "interlux/agents")
    private val main = Handler(Looper.getMainLooper())

    init {
        channel.setMethodCallHandler(this)
    }

    private fun ok(result: MethodChannel.Result, value: Any?) {
        main.post { result.success(value) }
    }

    private fun fail(result: MethodChannel.Result, code: String,
                     message: String?) {
        main.post { result.error(code, message, null) }
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        Thread {
            try {
                val res = when (call.method) {
                    "pairingList" -> AgentAuth.rpc(context, "pairing/list")
                    "pairingApprove" -> {
                        val id = call.argument<String>("id")
                        if (id.isNullOrEmpty()) {
                            throw AgentWs.RpcError(null, "missing id")
                        }
                        AgentAuth.rpc(context, "pairing/approve",
                            mapOf("id" to id))
                    }
                    "pairingDeny" -> {
                        val id = call.argument<String>("id")
                        if (id.isNullOrEmpty()) {
                            throw AgentWs.RpcError(null, "missing id")
                        }
                        AgentAuth.rpc(context, "pairing/deny",
                            mapOf("id" to id))
                    }
                    "pairingRevoke" -> {
                        val agent = call.argument<String>("agentId")
                        if (agent.isNullOrEmpty()) {
                            throw AgentWs.RpcError(null, "missing agentId")
                        }
                        AgentAuth.rpc(context, "pairing/revoke",
                            mapOf("agent_id" to agent))
                    }
                    else -> {
                        fail(result, "NOT_FOUND", "unknown agents method")
                        return@Thread
                    }
                }
                if (res == null) {
                    fail(result, "PAIRING_REQUIRED",
                        "another owner holds the store")
                } else {
                    ok(result, jsonToDart(res))
                }
            } catch (e: AgentWs.RpcError) {
                fail(result, "RPC_FAILED", e.message)
            } catch (e: Exception) {
                fail(result, "FAILED", e.message)
            }
        }.also { it.isDaemon = true; it.start() }
    }

    private fun jsonToDart(value: Any?): Any? {
        return when (value) {
            null, is String, is Boolean, is Number -> value
            is JSONObject -> {
                val out = HashMap<String, Any?>()
                val keys = value.keys()
                while (keys.hasNext()) {
                    val k = keys.next()
                    out[k] = jsonToDart(
                        if (value.isNull(k)) null else value.opt(k))
                }
                out
            }
            is org.json.JSONArray -> {
                (0 until value.length()).map { i ->
                    jsonToDart(if (value.isNull(i)) null else value.opt(i))
                }
            }
            else -> value.toString()
        }
    }
}
