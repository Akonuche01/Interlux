import 'package:xterm/xterm.dart';

/// URL tap-to-open support.
///
/// A tap carrying a [CellOffset] (from `TerminalView.onTapUp`) resolves to an
/// absolute buffer line — no pixel math needed. Only `http`/`https` links
/// ever open, and only after an explicit confirm dialog (no auto-launch).
///
/// Limits (v1): single-line links plus wrapped continuations (up to 3 extra
/// lines), main buffer only.
final RegExp _urlPattern = RegExp(r'https?://[^\s<>"]+');

/// Joins the tapped line with following wrapped lines into one logical
/// line (a wrapping line overflows onto the next), then matches once.
String? findLinkAt(Terminal terminal, CellOffset at, {int maxSpan = 4}) {
  final lines = terminal.mainBuffer.lines;
  if (at.y < 0 || at.y >= lines.length) return null;
  var logical = lines[at.y].getText();
  if (at.x < 0 || at.x >= logical.length) return null;

  var span = at.y;
  while (span + 1 < lines.length &&
      span - at.y + 1 < maxSpan &&
      lines[span].isWrapped) {
    span++;
    logical += lines[span].getText().trimLeft();
  }

  for (final m in _urlPattern.allMatches(logical)) {
    if (m.start <= at.x && at.x < m.end) return _sanitize(m.group(0));
  }
  return null;
}

/// Keep only http/https; strip trailing punctuation the regex may swallow.
String? _sanitize(String? raw) {
  if (raw == null) return null;
  var url = raw.replaceAll(RegExp(r'[.,;:!?)\]]+$'), '');
  final uri = Uri.tryParse(url);
  if (uri == null) return null;
  if (uri.scheme != 'http' && uri.scheme != 'https') return null;
  if (uri.host.isEmpty) return null;
  return url;
}
