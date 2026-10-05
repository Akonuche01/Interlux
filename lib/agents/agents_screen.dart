import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

class _PendingRequest {
  final String id;
  final String label;
  final String code;
  _PendingRequest({
    required this.id,
    required this.label,
    required this.code,
  });
}

class _PairedAgent {
  final String agentId;
  final String label;
  final bool owner;
  final String created;
  _PairedAgent({
    required this.agentId,
    required this.label,
    required this.owner,
    required this.created,
  });
}

class AgentsScreen extends StatefulWidget {
  const AgentsScreen({super.key});

  @override
  State<AgentsScreen> createState() => _AgentsScreenState();
}

class _AgentsScreenState extends State<AgentsScreen> {
  static const _channel = MethodChannel('interlux/agents');

  List<_PendingRequest> _pending = [];
  List<_PairedAgent> _agents = [];
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  Future<void> _refresh() async {
    // Guard here, not at each call site. _approve, _deny and _revoke all await
    // a platform call and then call this, so the State can be disposed while
    // that call is in flight (tap Approve, press Back). The mounted checks
    // further down only protect the post-await setState calls -- this first
    // one ran unguarded and threw "setState() called after dispose()".
    if (!mounted) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final res = await _channel.invokeMapMethod<String, dynamic>(
        'pairingList',
      );
      if (!mounted) return;
      if (res == null) {
        setState(() {
          _loading = false;
          _error = 'Pairing required — another owner holds the store.';
        });
        return;
      }
      final pending = <_PendingRequest>[];
      for (final item in (res['pending'] as List? ?? [])) {
        final m = item as Map;
        pending.add(_PendingRequest(
          id: m['id'] as String? ?? '',
          label: m['label'] as String? ?? 'unknown app',
          code: m['code'] as String? ?? '',
        ));
      }
      final agents = <_PairedAgent>[];
      for (final item in (res['agents'] as List? ?? [])) {
        final m = item as Map;
        agents.add(_PairedAgent(
          agentId: m['agent_id'] as String? ?? '',
          label: m['label'] as String? ?? 'unknown app',
          owner: m['owner'] as bool? ?? false,
          created: m['created'] as String? ?? '',
        ));
      }
      setState(() {
        _pending = pending;
        _agents = agents;
        _loading = false;
      });
    } on PlatformException catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = e.message ?? 'Failed to load agents.';
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = 'Failed to load agents.';
      });
    }
  }

  Future<void> _approve(String id) async {
    try {
      await _channel.invokeMethod('pairingApprove', {'id': id});
      await _refresh();
    } on PlatformException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(e.message ?? 'Approve failed.')),
      );
    }
  }

  Future<void> _deny(String id) async {
    try {
      await _channel.invokeMethod('pairingDeny', {'id': id});
      await _refresh();
    } on PlatformException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(e.message ?? 'Deny failed.')),
      );
    }
  }

  Future<void> _revoke(String agentId) async {
    final confirm = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF1A1A1A),
        title: const Text('Revoke agent?',
            style: TextStyle(color: Color(0xFFE6E6E6))),
        content: Text(
          'This will revoke $agentId and drop its live connections.',
          style: const TextStyle(color: Color(0xFF999999)),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Revoke',
                style: TextStyle(color: Color(0xFFFF6B6B))),
          ),
        ],
      ),
    );
    if (confirm != true) return;
    try {
      await _channel.invokeMethod('pairingRevoke', {'agentId': agentId});
      await _refresh();
    } on PlatformException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(e.message ?? 'Revoke failed.')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        backgroundColor: const Color(0xFF111111),
        title: const Text('Agents',
            style: TextStyle(color: Color(0xFFE6E6E6))),
        iconTheme: const IconThemeData(color: Color(0xFFE6E6E6)),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh, size: 20),
            color: const Color(0xFFE6E6E6),
            onPressed: _refresh,
          ),
        ],
      ),
      body: _buildBody(),
    );
  }

  Widget _buildBody() {
    if (_loading) {
      return const Center(
        child: CircularProgressIndicator(color: Color(0xFF6B4FA1)),
      );
    }
    if (_error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(
            _error!,
            style: const TextStyle(color: Color(0xFF999999), fontSize: 14),
            textAlign: TextAlign.center,
          ),
        ),
      );
    }
    return ListView(
      padding: const EdgeInsets.all(12),
      children: [
        if (_pending.isNotEmpty) ...[
          const Padding(
            padding: EdgeInsets.only(bottom: 8),
            child: Text('Pending requests',
                style: TextStyle(
                    color: Color(0xFFE6E6E6),
                    fontSize: 14,
                    fontWeight: FontWeight.bold)),
          ),
          for (final p in _pending) _pendingCard(p),
          const SizedBox(height: 16),
        ],
        const Padding(
          padding: EdgeInsets.only(bottom: 8),
          child: Text('Paired agents',
              style: TextStyle(
                  color: Color(0xFFE6E6E6),
                  fontSize: 14,
                  fontWeight: FontWeight.bold)),
        ),
        if (_agents.isEmpty)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 16),
            child: Text('No agents paired yet.',
                style: TextStyle(color: Color(0xFF777777), fontSize: 13)),
          ),
        for (final a in _agents) _agentCard(a),
      ],
    );
  }

  Widget _pendingCard(_PendingRequest p) {
    return Card(
      color: const Color(0xFF1A1A1A),
      margin: const EdgeInsets.only(bottom: 8),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(p.label,
                style: const TextStyle(
                    color: Color(0xFFE6E6E6),
                    fontSize: 14,
                    fontWeight: FontWeight.bold)),
            const SizedBox(height: 4),
            // The confirmation code is the whole security ceremony:
            // the user reads it off the requesting device and matches
            // it before approving. It must never be small, dim, or
            // absent.
            Text(p.code,
                style: const TextStyle(
                    color: Color(0xFFE6E6E6),
                    fontSize: 28,
                    fontWeight: FontWeight.bold,
                    letterSpacing: 6)),
            const SizedBox(height: 8),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                TextButton(
                  onPressed: () => _deny(p.id),
                  child: const Text('Deny',
                      style: TextStyle(color: Color(0xFFFF6B6B))),
                ),
                const SizedBox(width: 8),
                ElevatedButton(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF6B4FA1),
                  ),
                  onPressed: () => _approve(p.id),
                  child: const Text('Approve'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _agentCard(_PairedAgent a) {
    return Card(
      color: const Color(0xFF1A1A1A),
      margin: const EdgeInsets.only(bottom: 8),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Text(a.label,
                          style: const TextStyle(
                              color: Color(0xFFE6E6E6),
                              fontSize: 14,
                              fontWeight: FontWeight.bold)),
                      if (a.owner) ...[
                        const SizedBox(width: 8),
                        Container(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 6, vertical: 2),
                          decoration: BoxDecoration(
                            color: const Color(0xFF6B4FA1),
                            borderRadius: BorderRadius.circular(4),
                          ),
                          child: const Text('owner',
                              style: TextStyle(
                                  color: Colors.white, fontSize: 10)),
                        ),
                      ],
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(a.created,
                      style: const TextStyle(
                          color: Color(0xFF777777), fontSize: 12)),
                ],
              ),
            ),
            if (!a.owner)
              IconButton(
                icon: const Icon(Icons.delete_outline,
                    size: 18, color: Color(0xFFFF6B6B)),
                onPressed: () => _revoke(a.agentId),
              ),
          ],
        ),
      ),
    );
  }
}
