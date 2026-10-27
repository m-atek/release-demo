// Today's orders from orders-service's /api/orders. Plain dart:io, so the app
// needs no packages beyond Flutter.

import 'dart:convert';
import 'dart:io';

class Order {
  const Order({
    required this.id,
    required this.store,
    required this.items,
    required this.totalEur,
    required this.status,
  });

  factory Order.fromJson(Map<String, dynamic> json) => Order(
    id: json['id'] as String,
    store: json['store'] as String,
    items: (json['items'] as List).cast<String>(),
    totalEur: (json['total_eur'] as num).toDouble(),
    status: json['status'] as String,
  );

  final String id;
  final String store;
  final List<String> items;
  final double totalEur;
  final String status;
}

class TodaysOrders {
  const TodaysOrders({required this.date, required this.orders});

  factory TodaysOrders.fromJson(Map<String, dynamic> json) => TodaysOrders(
    date: json['date'] as String,
    orders: (json['orders'] as List).map((order) => Order.fromJson(order as Map<String, dynamic>)).toList(),
  );

  final String date;
  final List<Order> orders;
}

typedef OrdersLoader = Future<TodaysOrders> Function();

/// Loads today's orders from orders-service at [baseUrl].
OrdersLoader httpOrders(String baseUrl, {Duration timeout = const Duration(seconds: 10)}) {
  return () async {
    final client = HttpClient()..connectionTimeout = timeout;
    try {
      final base = baseUrl.endsWith('/') ? baseUrl.substring(0, baseUrl.length - 1) : baseUrl;
      final request = await client.getUrl(Uri.parse('$base/api/orders'));
      final response = await request.close().timeout(timeout);
      final body = await response.transform(utf8.decoder).join();
      if (response.statusCode != 200) {
        throw HttpException('orders-service answered ${response.statusCode}');
      }
      return TodaysOrders.fromJson(jsonDecode(body) as Map<String, dynamic>);
    } finally {
      client.close(force: true);
    }
  };
}
