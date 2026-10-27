import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:store_floor/orders.dart';

Order order(String id, List<String> items) =>
    Order(id: id, store: 'Utrecht Centraal', items: items, totalEur: 10, status: 'packed');

void main() {
  test('parses /api/orders as orders-service sends it', () {
    const body =
        '{"date": "2026-10-27", "orders": [{"id": "261027-001", '
        '"store": "Rotterdam Blaak", "items": ["Rain jacket", "Day backpack"], '
        '"total_eur": 123.5, "status": "picking"}]}';
    final orders = TodaysOrders.fromJson(jsonDecode(body) as Map<String, dynamic>);
    expect(orders.date, '2026-10-27');
    expect(orders.orders.single.store, 'Rotterdam Blaak');
    expect(orders.orders.single.items, ['Rain jacket', 'Day backpack']);
    expect(orders.orders.single.totalEur, 123.5);
  });

  test('a product ordered three times or more is a stock alert', () {
    final alerts = stockAlerts([
      order('1', ['Rain jacket', 'Water bottle']),
      order('2', ['Rain jacket']),
      order('3', ['Rain jacket', 'Water bottle']),
      order('4', ['Water bottle', 'Day backpack']),
    ]);
    expect(alerts.map((a) => '${a.product} ${a.ordered}'), ['Rain jacket 3', 'Water bottle 3']);
  });

  test('no alerts on a quiet day', () {
    expect(
      stockAlerts([
        order('1', ['Rain jacket']),
      ]),
      isEmpty,
    );
  });

  test('loads orders over HTTP and reports a failing service', () async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    server.listen((request) {
      if (request.uri.path == '/api/orders') {
        request.response.write('{"date": "2026-10-27", "orders": []}');
      } else {
        request.response.statusCode = 404;
      }
      request.response.close();
    });
    try {
      final orders = await httpOrders('http://127.0.0.1:${server.port}/')();
      expect(orders.orders, isEmpty);
      final missing = httpOrders('http://127.0.0.1:${server.port}/nowhere');
      await expectLater(missing(), throwsA(isA<HttpException>()));
    } finally {
      await server.close(force: true);
    }
  });
}
