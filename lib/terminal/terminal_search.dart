import 'package:xterm/xterm.dart';

/// One hit of a scrollback search: buffer line + char range.
///
/// Positions are relative to the buffer at scan time. Callers re-scan on
/// every navigation press (cheap: a few thousand short strings), so hits
/// never go stale as output arrives.
///
/// Limits (v1): plain case-insensitive substring, main buffer only (alt
/// screen of fullscreen apps is skipped), no matches spanning wrapped lines,
/// no auto-scroll — the current hit is highlighted via selection and the
/// user finger-scrolls to it.
class TerminalMatch {
  final int line;
  final int start;
  final int end;

  const TerminalMatch({
    required this.line,
    required this.start,
    required this.end,
  });
}

/// Scan the main scrollback buffer for [query]. Oldest match first.
List<TerminalMatch> findTerminalMatches(
  Terminal terminal,
  String query, {
  int maxMatches = 1000,
}) {
  if (query.isEmpty) return const [];
  final needle = query.toLowerCase();
  final lines = terminal.mainBuffer.lines;
  final matches = <TerminalMatch>[];
  for (var i = 0; i < lines.length && matches.length < maxMatches; i++) {
    final text = lines[i].getText().toLowerCase();
    var from = 0;
    while (matches.length < maxMatches) {
      final at = text.indexOf(needle, from);
      if (at < 0) break;
      matches.add(TerminalMatch(line: i, start: at, end: at + needle.length));
      from = at + needle.length;
    }
  }
  return matches;
}

/// Highlight [match] through the view controller's selection.
///
/// Returns false when the line moved out of range since the scan (caller
/// should re-scan); never throws.
bool highlightTerminalMatch(
  Terminal terminal,
  TerminalController controller,
  TerminalMatch match,
) {
  final lines = terminal.mainBuffer.lines;
  if (match.line < 0 || match.line >= lines.length) return false;
  final line = lines[match.line];
  final text = line.getText();
  if (match.end > text.length) return false;
  controller.setSelection(
    CellAnchor(match.start, owner: line),
    CellAnchor(match.end, owner: line),
  );
  return true;
}
