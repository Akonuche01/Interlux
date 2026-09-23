import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Battery-optimizations prompt (Phase 1.2).
///
/// Shown once on cold start when the app is battery-optimized: Doze then
/// throttles our sockets (proven mid-test: TCP timeouts + DNS EPERM with
/// the screen off). "Allow" opens the system exemption screen; "Later"
/// remembers the dismissal.
class BatteryOptPrompt {
  static const _channel = MethodChannel('interlux/power');
  static const _dismissKey = 'battery_opt_dismissed';

  static Future<void> maybeShow(BuildContext context) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      if (prefs.getBool(_dismissKey) ?? false) return;
      final ignoring =
          await _channel.invokeMethod<bool>(
            'isIgnoringBatteryOptimizations',
          ) ??
          true;
      if (ignoring) return;
    } catch (_) {
      return;
    }
    if (!context.mounted) return;
    final action = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: const Color(0xFF1A1A1A),
        title: const Text(
          'Keep sessions alive',
          style: TextStyle(color: Colors.white),
        ),
        content: const Text(
          'Android pauses network access for battery-optimized apps when '
          'the screen is off — downloads stall and shells go quiet. '
          'Exempt Interlux to keep long scans and sessions running.',
          style: TextStyle(color: Color(0xFFE6E6E6), fontSize: 13),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, 'later'),
            child: const Text('Later'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, 'allow'),
            child: const Text('Allow'),
          ),
        ],
      ),
    );
    final prefs = await SharedPreferences.getInstance();
    if (action == 'allow') {
      try {
        await _channel.invokeMethod<bool>(
          'requestIgnoreBatteryOptimizations',
        );
      } catch (_) {
        // System screen unavailable; stay optimized and stop asking.
      }
      await prefs.setBool(_dismissKey, true);
    } else if (action == 'later') {
      await prefs.setBool(_dismissKey, true);
    }
  }
}
