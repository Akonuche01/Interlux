import 'package:flutter_test/flutter_test.dart';
import 'package:interlux/terminal/terminal_search.dart';
import 'package:xterm/xterm.dart';

Terminal _terminalWith(String text) {
  final term = Terminal(maxLines: 100);
  term.write(text);
  return term;
}

void main() {
  group('findTerminalMatches', () {
    test('finds all hits oldest-first with columns', () {
      final term = _terminalWith('hello world\nfoo bar\nhello again\n');
      final matches = findTerminalMatches(term, 'hello');
      expect(matches.length, 2);
      expect(matches[0].start, 0);
      expect(matches[0].end, 5);
      expect(matches[1].start, 0);
      expect(matches[1].end, 5);
      expect(matches[1].line, greaterThan(matches[0].line));
    });

    test('is case-insensitive and finds mid-line hits', () {
      final term = _terminalWith('say HELLO twice hello\n');
      final matches = findTerminalMatches(term, 'hello');
      expect(matches.length, 2);
      expect(matches[0].start, 4);
      expect(matches[1].start, 16);
    });

    test('empty query and misses return empty', () {
      final term = _terminalWith('some text\n');
      expect(findTerminalMatches(term, ''), isEmpty);
      expect(findTerminalMatches(term, 'zzz'), isEmpty);
    });

    test('respects maxMatches', () {
      final term = _terminalWith('a a a a a\n');
      expect(findTerminalMatches(term, 'a', maxMatches: 3).length, 3);
    });
  });

  group('highlightTerminalMatch', () {
    test('highlights through controller selection', () {
      final term = _terminalWith('hello world\n');
      final controller = TerminalController();
      final matches = findTerminalMatches(term, 'world');
      expect(matches.length, 1);
      expect(highlightTerminalMatch(term, controller, matches.single), isTrue);
      expect(controller.selection, isNotNull);
      controller.dispose();
    });

    test('returns false out of range without throwing', () {
      final term = _terminalWith('hi\n');
      final controller = TerminalController();
      const stale = TerminalMatch(line: 9999, start: 0, end: 2);
      expect(highlightTerminalMatch(term, controller, stale), isFalse);
      controller.dispose();
    });
  });
}
