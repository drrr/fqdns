#!/usr/bin/env python
# thanks @phuslu https://github.com/phus/dnsproxy/blob/master/dnsproxy.py
# thanks @ofmax https://github.com/madeye/gaeproxy/blob/master/assets/modules/python.mp3
import argparse
import socket
import logging
import logging.handlers
import sys
import select
import contextlib
import time
import struct
import json
import random

import dpkt
import gevent.server
import gevent.queue
import gevent.monkey


LOGGER = logging.getLogger('fqdns')

ERROR_NO_DATA = 11
SO_MARK = 36
OUTBOUND_MARK = 0
OUTBOUND_IP = None


def main():
    global OUTBOUND_MARK
    global OUTBOUND_IP
    gevent.monkey.patch_all(dns=gevent.version_info[0] >= 1, thread=False)
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument('--log-file')
    argument_parser.add_argument('--log-level', choices=['INFO', 'DEBUG'], default='INFO')
    argument_parser.add_argument('--outbound-mark', help='for example 0xcafe, set to every packet send out',
                                 default='0')
    argument_parser.add_argument('--outbound-ip', help='the ip address for every packet send out')
    sub_parsers = argument_parser.add_subparsers()
    resolve_parser = sub_parsers.add_parser('resolve', help='start as dns client')
    resolve_parser.add_argument('domain', help='one or more domain names to query', nargs='+')
    resolve_parser.add_argument(
        '--at', help='one or more dns servers', default=[], action='append')
    resolve_parser.add_argument(
        '--strategy', help='anti-GFW strategy, for UDP only', default='pick-right',
        choices=['pick-first', 'pick-later', 'pick-right', 'pick-right-later', 'pick-all'])
    resolve_parser.add_argument(
        '--wrong-answer', help='wrong answer forged by GFW, for UDP only', action='append')
    resolve_parser.add_argument('--timeout', help='in seconds', default=1, type=float)
    resolve_parser.add_argument('--server-type', default='udp', choices=['udp', 'tcp'])
    resolve_parser.add_argument('--record-type', default='A', choices=['A', 'TXT'])
    resolve_parser.add_argument('--retry', default=1, type=int)
    resolve_parser.set_defaults(handler=resolve)
    discover_parser = sub_parsers.add_parser('discover', help='resolve black listed domain to discover wrong answers')
    discover_parser.add_argument('--at', help='dns server', default='8.8.8.8:53')
    discover_parser.add_argument('--timeout', help='in seconds', default=1, type=float)
    discover_parser.add_argument('--repeat', help='repeat query for each domain many times', default=30, type=int)
    discover_parser.add_argument('--only-new', help='only show the new wrong answers', action='store_true')
    discover_parser.add_argument(
        '--domain', help='black listed domain such as twitter.com', default=[], action='append')
    discover_parser.set_defaults(handler=discover)
    serve_parser = sub_parsers.add_parser('serve', help='start as dns server')
    serve_parser.add_argument('--listen', help='local address bind to', default='*:53')
    serve_parser.add_argument(
        '--upstream', help='upstream dns server forwarding to for non china domain', default=[], action='append')
    serve_parser.add_argument(
        '--china-upstream', help='upstream dns server forwarding to for china domain', default=[], action='append')
    serve_parser.add_argument(
        '--hosted-domain', help='the domain a.com will be transformed to a.com.b.com', default=[], action='append')
    serve_parser.add_argument(
        '--hosted-at', help='the domain b.com will host a.com.b.com', default='fqrouter.com')
    serve_parser.add_argument(
        '--direct', help='direct forward to first upstream via UDP', action='store_true')
    serve_parser.add_argument(
        '--enable-china-domain', help='otherwise china domain will not query against china-upstreams',
        action='store_true')
    serve_parser.add_argument(
        '--enable-hosted-domain', help='otherwise hosted domain will not query with suffix hosted-at',
        action='store_true')
    serve_parser.add_argument(
        '--fallback-timeout', help='fallback from udp to tcp after timeout, in seconds', default=1)
    serve_parser.add_argument(
        '--strategy', help='anti-GFW strategy, for UDP only', default='pick-right',
        choices=['pick-first', 'pick-later', 'pick-right', 'pick-right-later', 'pick-all'])
    serve_parser.set_defaults(handler=serve)
    args = argument_parser.parse_args()
    OUTBOUND_MARK = eval(args.outbound_mark)
    OUTBOUND_IP = args.outbound_ip
    log_level = getattr(logging, args.log_level)
    logging.basicConfig(stream=sys.stdout, level=log_level, format='%(asctime)s %(levelname)s %(message)s')
    if args.log_file:
        handler = logging.handlers.RotatingFileHandler(
            args.log_file, maxBytes=1024 * 256, backupCount=0)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        handler.setLevel(log_level)
        logging.getLogger('fqdns').addHandler(handler)
    return_value = args.handler(**{k: getattr(args, k) for k in vars(args) \
                                   if k not in {'handler', 'log_file', 'log_level', 'outbound_mark', 'outbound_ip'}})
    sys.stderr.write(json.dumps(return_value))
    sys.stderr.write('\n')


def serve(listen, upstream, china_upstream, hosted_domain, hosted_at,
          direct, enable_china_domain, enable_hosted_domain, fallback_timeout, strategy):
    address = parse_ip_colon_port(listen)
    upstreams = [parse_ip_colon_port(e) for e in upstream] or \
                [('8.8.8.8', 53), ('208.67.222.222', 5353)]
    if enable_china_domain:
        china_upstreams = [parse_ip_colon_port(e) for e in china_upstream] or \
                          [('114.114.114.114', 53), ('114.114.115.115', 53)]
    else:
        china_upstreams = []
    if enable_hosted_domain:
        hosted_domains = hosted_domain or HOSTED_DOMAINS()
    else:
        hosted_domains = set()
    server = DNSServer(address, upstreams, china_upstreams,
                       hosted_domains, hosted_at, direct, fallback_timeout, strategy)
    LOGGER.info('dns server started at %r, forwarding to %r', address, upstreams)
    try:
        server.serve_forever()
    except:
        LOGGER.exception('dns server failed')
    finally:
        LOGGER.info('dns server stopped')


class DNSServer(gevent.server.DatagramServer):
    def __init__(self, address, upstreams, china_upstreams,
                 hosted_domains, hosted_at, direct, fallback_timeout, strategy):
        super(DNSServer, self).__init__(address)
        self.upstreams = upstreams
        self.china_upstreams = china_upstreams
        self.hosted_domains = hosted_domains
        self.hosted_at = hosted_at
        self.direct = direct
        self.fallback_timeout = fallback_timeout
        self.strategy = strategy

    def handle(self, raw_request, address):
        request = dpkt.dns.DNS(raw_request)
        LOGGER.debug('received downstream request from %s: %s' % (str(address), repr(request)))
        domains = [question.name for question in request.qd if dpkt.dns.DNS_A == question.type]
        if len(domains) == 1 and not self.direct:
            domain = domains[0]
            response = dpkt.dns.DNS(raw_request)
            if not self.query_smartly(domain, response):
                return # let client retry
        else:
            response = self.query_first_upstream_via_udp(request)
        LOGGER.debug('forward to downstream response to %s: %s' % (str(address), repr(response)))
        self.sendto(str(response), address)

    def query_smartly(self, domain, response):
        selected_upstreams = self.china_upstreams if \
            self.china_upstreams and is_china_domain(domain) else self.upstreams
        if domain.startswith('ignore-hosted-domain.'):
            querying_domain = domain.replace('ignore-hosted-domain.', '')
        else:
            querying_domain = '%s.%s' % (domain, self.hosted_at) if domain in self.hosted_domains else domain
        answers = resolve(dpkt.dns.DNS_A, [querying_domain], 'udp',
                          selected_upstreams, self.fallback_timeout, strategy=self.strategy).get(querying_domain)
        if not answers:
            answers = resolve(
                dpkt.dns.DNS_A, [querying_domain], 'tcp',
                selected_upstreams, self.fallback_timeout * 2).get(querying_domain)
            if not answers:
                return False
        response.set_qr(True)
        response.an = [dpkt.dns.DNS.RR(
            name=domain, type=dpkt.dns.DNS_A, ttl=3600,
            rlen=len(socket.inet_aton(answer)),
            rdata=socket.inet_aton(answer)) for answer in answers]
        return True

    def query_first_upstream_via_udp(self, request):
        sock = create_socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
        with contextlib.closing(sock):
            sock.sendto(str(request), self.upstreams[0])
            return dpkt.dns.DNS(sock.recv(512))


def resolve(record_type, domain, server_type, at, timeout, strategy='pick-right', wrong_answer=(), retry=1):
    if isinstance(record_type, basestring):
        record_type = getattr(dpkt.dns, 'DNS_%s' % record_type)
    servers = [parse_ip_colon_port(e) for e in at] or [('8.8.8.8', 53)]
    domains = set(domain)
    domains_answers = {}
    for i in range(retry):
        domains_answers.update(resolve_once(
            record_type, domains, server_type, servers, timeout, strategy, wrong_answer))
        domains = domains - set(domains_answers.keys())
        if domains:
            LOGGER.warn('did not finish resolving: %s' % domains)
        else:
            return domains_answers
    return domains_answers


def resolve_once(record_type, domains, server_type, servers, timeout, strategy, wrong_answer):
    greenlets = []
    queue = gevent.queue.Queue()
    try:
        for domain in domains:
            for server in servers:
                server_ip, server_port = server
                greenlets.append(gevent.spawn(
                    resolve_one, record_type, domain, server_type,
                    server_ip, server_port, timeout - 0.1, strategy, wrong_answer, queue=queue))
        started_at = time.time()
        domains_answers = {}
        remaining_timeout = started_at + timeout - time.time()
        while remaining_timeout > 0:
            try:
                domain, answers = queue.get(timeout=remaining_timeout)
                domains_answers[domain] = answers
                if len(domains_answers) == len(domains):
                    return domains_answers
            except gevent.queue.Empty:
                return domains_answers
            remaining_timeout = started_at + timeout - time.time()
        return domains_answers
    finally:
        for greenlet in greenlets:
            greenlet.kill(block=False)


def parse_ip_colon_port(ip_colon_port):
    if not isinstance(ip_colon_port, basestring):
        return ip_colon_port
    if ':' in ip_colon_port:
        server_ip, server_port = ip_colon_port.split(':')
        server_port = int(server_port)
    else:
        server_ip = ip_colon_port
        server_port = 53
    return '' if '*' == server_ip else server_ip, server_port


def resolve_one(record_type, domain, server_type, server_ip, server_port, timeout, strategy, wrong_answer, queue=None):
    answers = []
    try:
        LOGGER.info('%s resolve %s at %s:%s' % (server_type, domain, server_ip, server_port))
        if 'udp' == server_type:
            wrong_answers = set(wrong_answer) if wrong_answer else set()
            wrong_answers |= BUILTIN_WRONG_ANSWERS()
            answers = resolve_over_udp(
                record_type, domain, server_ip, server_port, timeout, strategy, wrong_answers)
        elif 'tcp' == server_type:
            answers = resolve_over_tcp(record_type, domain, server_ip, server_port, timeout)
        else:
            LOGGER.error('unsupported server type: %s' % server_type)
    except:
        LOGGER.exception('failed to resolve one: %s' % domain)
    if answers and queue:
        queue.put((domain, answers))
    LOGGER.info('%s resolved %s at %s:%s => %s' % (server_type, domain, server_ip, server_port, json.dumps(answers)))
    return answers


def resolve_over_tcp(record_type, domain, server_ip, server_port, timeout):
    sock = create_socket(family=socket.AF_INET, type=socket.SOCK_STREAM)
    with contextlib.closing(sock):
        sock.setblocking(0)
        request = dpkt.dns.DNS(id=get_transaction_id(), qd=[dpkt.dns.DNS.Q(name=domain, type=record_type)])
        LOGGER.debug('send request: %s' % repr(request))
        sock.settimeout(1)
        try:
            sock.connect((server_ip, server_port))
        except gevent.GreenletExit:
            return []
        except:
            LOGGER.exception('failed to connect to %s:%s due to %s' % (server_ip, server_port, sys.exc_info()[1]))
            return []
        sock.settimeout(None)
        data = str(request)
        sock.send(struct.pack('>h', len(data)) + data)
        try:
            ins, outs, errors = select.select([sock], [], [sock], timeout)
        except gevent.GreenletExit:
            return []
        if errors:
            LOGGER.error('failed to read dns response')
            return []
        if not ins:
            return []
        rfile = sock.makefile('r', 512)
        data = rfile.read(2)
        data = rfile.read(struct.unpack('>h', data)[0])
        response = dpkt.dns.DNS(data)
        if not is_right_response(response, BUILTIN_WRONG_ANSWERS()): # filter opendns "nxdomain"
            response = None
        if response:
            if dpkt.dns.DNS_A == record_type:
                return list_ipv4_addresses(response)
            else:
                return [answer.rdata for answer in response.an]
        else:
            return []


def resolve_over_udp(record_type, domain, server_ip, server_port, timeout, strategy, wrong_answers):
    sock = create_socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
    with contextlib.closing(sock):
        sock.setblocking(0)
        request = dpkt.dns.DNS(id=get_transaction_id(), qd=[dpkt.dns.DNS.Q(name=domain, type=record_type)])
        LOGGER.debug('send request: %s' % repr(request))
        sock.sendto(str(request), (server_ip, server_port))
        if dpkt.dns.DNS_A == record_type:
            responses = pick_responses(sock, timeout, strategy, wrong_answers)
            if len(responses) == 1:
                return list_ipv4_addresses(responses[0])
            elif len(responses) > 1:
                return [list_ipv4_addresses(response) for response in responses]
            else:
                return []
        else:
            try:
                response = dpkt.dns.DNS(receive(sock, time.time() + timeout))
                LOGGER.debug('received response: %s' % repr(response))
                return [answer.rdata for answer in response.an]
            except SocketTimeout:
                return []


def get_transaction_id():
    return random.randint(1, 65535)


def receive(sock, deadline, size=512):
    remaining_timeout = deadline - time.time()
    while remaining_timeout > 0:
        LOGGER.debug('wait for max %s seconds' % remaining_timeout)
        try:
            ins, outs, errors = select.select([sock], [], [sock], remaining_timeout)
        except gevent.GreenletExit:
            raise SocketTimeout()
        if errors:
            LOGGER.error('failed to receive')
            raise Exception('failed to receive')
        if sock not in ins:
            raise SocketTimeout()
        try:
            return sock.recv(size)
        except socket.error, e:
            if ERROR_NO_DATA == e[0]:
                remaining_timeout = deadline - time.time()
                continue
            raise
    raise SocketTimeout()


def pick_responses(sock, timeout, strategy, wrong_answers):
    picked_responses = []
    started_at = time.time()
    deadline = started_at + timeout
    remaining_timeout = deadline - time.time()
    while remaining_timeout > 0:
        try:
            response = dpkt.dns.DNS(receive(sock, deadline))
        except SocketTimeout:
            return picked_responses
        LOGGER.debug('received response: %s' % repr(response))
        if 'pick-first' == strategy:
            return [response]
        if 'pick-all' != strategy and len(response.an) > 1:
            return [response] # GFW does not forge multiple answers
        if 'pick-later' == strategy:
            picked_responses = [response]
        elif 'pick-right' == strategy:
            if is_right_response(response, wrong_answers):
                return [response]
        elif 'pick-right-later' == strategy:
            if is_right_response(response, wrong_answers):
                picked_responses = [response]
        elif 'pick-all' == strategy:
            picked_responses.append(response)
        else:
            raise Exception('unsupported strategy: %s' % strategy)
        remaining_timeout = started_at + timeout - time.time()
    return picked_responses


def is_right_response(response, wrong_answers):
    answers = list_ipv4_addresses(response)
    if not answers: # GFW can forge empty response
        return False
    if len(answers) > 1: # GFW does not forge response with more than one answer
        return True
    return not any(answer in wrong_answers for answer in answers)


def list_ipv4_addresses(response):
    return [socket.inet_ntoa(answer.rdata) for answer in response.an if dpkt.dns.DNS_A == answer.type]


def discover(domain, at, timeout, repeat, only_new):
    server_ip, server_port = parse_ip_colon_port(at)
    domains = domain or [
        'facebook.com', 'youtube.com', 'twitter.com', 'plus.google.com', 'drive.google.com']
    wrong_answers = set()
    greenlets = []
    for domain in domains:
        right_answers = resolve_over_tcp(dpkt.dns.DNS_A, domain, server_ip, server_port, timeout * 2)
        right_answer = right_answers[0] if right_answers else None
        for i in range(repeat):
            greenlets.append(gevent.spawn(
                discover_one, domain, server_ip, server_port, timeout, right_answer))
    for greenlet in greenlets:
        wrong_answers |= greenlet.get()
    if only_new:
        return list(wrong_answers - BUILTIN_WRONG_ANSWERS())
    else:
        return list(wrong_answers)


def discover_one(domain, server_ip, server_port, timeout, right_answer):
    wrong_answers = set()
    responses_answers = resolve_over_udp(
        dpkt.dns.DNS_A, domain, server_ip, server_port, timeout, 'pick-all', set())
    contains_right_answer = any(len(answers) > 1 for answers in responses_answers)
    if right_answer or contains_right_answer:
        for answers in responses_answers:
            if len(answers) == 1 and answers[0] != right_answer:
                wrong_answers |= set(answers)
    return wrong_answers


def create_socket(*args, **kwargs):
    sock = socket.socket(*args, **kwargs)
    if OUTBOUND_MARK:
        sock.setsockopt(socket.SOL_SOCKET, SO_MARK, OUTBOUND_MARK)
    if OUTBOUND_IP:
        sock.bind((OUTBOUND_IP, 0))
    return sock


class SocketTimeout(BaseException):
    pass


def BUILTIN_WRONG_ANSWERS():
    return {
        '4.36.66.178',
        '8.7.198.45',
        '37.61.54.158',
        '46.82.174.68',
        '59.24.3.173',
        '64.33.88.161',
        '64.33.99.47',
        '64.66.163.251',
        '65.104.202.252',
        '65.160.219.113',
        '66.45.252.237',
        '72.14.205.99',
        '72.14.205.104',
        '78.16.49.15',
        '93.46.8.89',
        '128.121.126.139',
        '159.106.121.75',
        '169.132.13.103',
        '192.67.198.6',
        '202.106.1.2',
        '202.181.7.85',
        '203.161.230.171',
        '203.98.7.65',
        '207.12.88.98',
        '208.56.31.43',
        '209.36.73.33',
        '209.145.54.50',
        '209.220.30.174',
        '211.94.66.147',
        '213.169.251.35',
        '216.221.188.182',
        '216.234.179.13',
        '243.185.187.39',
        # plus.google.com
        '74.125.127.102',
        '74.125.155.102',
        '74.125.39.113',
        '74.125.39.102',
        '209.85.229.138',
        # opendns
        '67.215.65.132'
    }


CHINA_DOMAINS = [
    '07073.com',
    '10010.com',
    '100ye.com',
    '114la.com',
    '115.com',
    '120ask.com',
    '126.com',
    '126.net',
    '1616.net',
    '163.com',
    '17173.com',
    '1778.com',
    '178.com',
    '17u.com',
    '19lou.com',
    '1o26.com',
    '1ting.com',
    '21cn.com',
    '2345.com',
    '265.com',
    '265g.com',
    '28.com',
    '28tui.com',
    '2hua.com',
    '2mdn.net',
    '315che.com',
    '3366.com',
    '360buy.com',
    '360buyimg.com',
    '360doc.com',
    '36kr.com',
    '39.net',
    '3dmgame.com',
    '4399.com',
    '4738.com',
    '500wan.com',
    '51.com',
    '51.la',
    '5173.com',
    '51auto.com',
    '51buy.com',
    '51cto.com',
    '51fanli.com',
    '51job.com',
    '52kmh.com',
    '52pk.net',
    '52tlbb.com',
    '53kf.com',
    '55bbs.com',
    '55tuan.com',
    '56.com',
    '58.com',
    '591hx.com',
    '5d6d.net',
    '61.com',
    '70e.com',
    '777wyx.com',
    '778669.com',
    '7c.com',
    '7k7k.com',
    '88db.com',
    '91.com',
    '99bill.com',
    'a135.net',
    'abang.com',
    'abchina.com',
    'ad1111.com',
    'admin5.com',
    'adnxs.com',
    'adobe.com',
    'adroll.com',
    'ads8.com',
    'adsame.com',
    'adsonar.com',
    'adtechus.com',
    'aibang.com',
    'aifang.com',
    'aili.com',
    'aipai.com',
    'aizhan.com',
    'ali213.net',
    'alibaba.com',
    'alicdn.com',
    'aliexpress.com',
    'alimama.com',
    'alipay.com',
    'alipayobjects.com',
    'alisoft.com',
    'alivv.com',
    'aliyun.com',
    'allyes.com',
    'amazon.com',
    'anjuke.com',
    'anzhi.com',
    'aol.com',
    'apple.com',
    'arpg2.com',
    'atdmt.com',
    'b2b168.com',
    'babytree.com',
    'baidu.com',
    'baihe.com',
    'baixing.com',
    'bankcomm.com',
    'baomihua.com',
    'bdimg.com',
    'bdstatic.com',
    'bendibao.com',
    'betrad.com',
    'bilibili.tv',
    'bing.com',
    'bitauto.com',
    'blog.163.com',
    'blogchina.com',
    'blueidea.com',
    'bluekai.com',
    'booksky.org',
    'caixin.com',
    'ccb.com',
    'ccidnet.com',
    'cctv*.com',
    'china.com',
    'chinabyte.com',
    'chinahr.com',
    'chinanews.com',
    'chinaunix.net',
    'chinaw3.com',
    'chinaz.com',
    'chuangelm.com',
    'ci123.com',
    'cmbchina.com',
    'cnbeta.com',
    'cnblogs.com',
    'cncn.com',
    'cnhubei.com',
    'cnki.net',
    'cnmo.com',
    'cnxad.com',
    'cnzz.com',
    'cocoren.com',
    'compete.com',
    'comsenz.com',
    'coo8.com',
    'cqnews.net',
    'crsky.com',
    'csdn.net',
    'ct10000.com',
    'ctrip.com',
    'dangdang.com',
    'daqi.com',
    'dayoo.com',
    'dbank.com',
    'ddmap.com',
    'dedecms.com',
    'dh818.com',
    'diandian.com',
    'dianping.com',
    'discuz.net',
    'doc88.com',
    'docin.com',
    'donews.com',
    'dospy.com',
    'douban.com',
    'douban.fm',
    'doubleclick.com',
    'doubleclick.net',
    'duba.net',
    'duote.com',
    'duowan.com',
    'dzwww.com',
    'eastday.com',
    'eastmoney.com',
    'ebay.com',
    'elong.com',
    'ename.net',
    'etao.com',
    'exam8.com',
    'eye.rs',
    'fantong.com',
    'fastcdn.com',
    'fblife.com',
    'fengniao.com',
    'fenzhi.com',
    'flickr.com',
    'fobshanghai.com',
    'ftuan.com',
    'funshion.com',
    'fx120.net',
    'game3737.com',
    'gamersky.com',
    'gamestlbb.com',
    'gamesville.com',
    'ganji.com',
    'gfan.com',
    'gongchang.com',
    'google-analytics.com',
    'gougou.com',
    'gtimg.com',
    'hao123.com',
    'haodf.com',
    'harrenmedianetwork.com',
    'hc360.com',
    'hefei.cc',
    'hf365.com',
    'hiapk.com',
    'hichina.com',
    'homeinns.com',
    'hotsales.net',
    'house365.com',
    'huaban.com',
    'huanqiu.com',
    'hudong.com',
    'hupu.com',
    'iask.com',
    'iciba.com',
    'icson.com',
    'ifeng.com',
    'iloveyouxi.com',
    'im286.com',
    'imanhua.com',
    'img.cctvpic.com',
    'imrworldwide.com',
    'invitemedia.com',
    'ip138.com',
    'ipinyou.com',
    'iqilu.com',
    'iqiyi.com',
    'irs01.com',
    'irs01.net',
    'it168.com',
    'iteye.com',
    'iyaya.com',
    'jb51.net',
    'jiathis.com',
    'jiayuan.com',
    'jing.fm',
    'jinti.com',
    'jqw.com',
    'jumei.com',
    'jxedt.com',
    'jysq.net',
    'kaixin001.com',
    'kandian.com',
    'kdnet.net',
    'kimiss.com',
    'ku6.com',
    'ku6cdn.com',
    'ku6img.com',
    'kuaidi100.com',
    'kugou.com',
    'l99.com',
    'lady8844.com',
    'lafaso.com',
    'lashou.com',
    'legolas-media.com',
    'lehecai.com',
    'leho.com',
    'letv.com',
    'liebiao.com',
    'lietou.com',
    'linezing.com',
    'linkedin.com',
    'live.com',
    'longhoo.net',
    'lusongsong.com',
    'lxdns.com',
    'lycos.com',
    'lygo.com',
    'm18.com',
    'm1905.com',
    'made-in-china.com',
    'makepolo.com',
    'mangocity.com',
    'manzuo.com',
    'mapbar.com',
    'mathtag.com',
    'mediaplex.com',
    'mediav.com',
    'meilele.com',
    'meilishuo.com',
    'meishichina.com',
    'meituan.com',
    'meizu.com',
    'miaozhen.com',
    'microsoft.com',
    'miercn.com',
    'mlt01.com',
    'mmstat.com',
    'mnwan.com',
    'mogujie.com',
    'mookie1.com',
    'moonbasa.com',
    'mop.com',
    'mosso.com',
    'mplife.com',
    'msn.com',
    'mtime.com',
    'mumayi.com',
    'mydrivers.com',
    'net114.com',
    'netease.com',
    'newsmth.net',
    'nipic.com',
    'nowec.com',
    'nuomi.com',
    'oadz.com',
    'oeeee.com',
    'onetad.com',
    'onlinedown.net',
    'onlylady.com',
    'oschina.net',
    'otwan.com',
    'paipai.com',
    'paypal.com',
    'pchome.net',
    'pcpop.com',
    'pengyou.com',
    'php100.com',
    'phpwind.net',
    'pingan.com',
    'pixlr.com',
    'pp.cc',
    'ppstream.com',
    'pptv.com',
    'ptlogin2.qq.com',
    'pubmatic.com',
    'q150.com',
    'qianlong.com',
    'qidian.com',
    'qingdaonews.com',
    'qire123.com',
    'qiushibaike.com',
    'qiyou.com',
    'qjy168.com',
    'qq.com',
    'qq937.com',
    'qstatic.com',
    'quantserve.com',
    'qunar.com',
    'rakuten.co.jp',
    'readnovel.com',
    'renren.com',
    'rtbidder.net',
    'scanscout.com',
    'scorecardresearch.com',
    'sdo.com',
    'seowhy.com',
    'serving-sys.com',
    'sf-express.com',
    'shangdu.com',
    'si.kz',
    'sina.com',
    'sinahk.net',
    'sinajs.com',
    'smzdm.com',
    'snyu.com',
    'sodu.org',
    'sogou.com',
    'sohu.com',
    'soku.com',
    'sootoo.com',
    'soso.com',
    'soufun.com',
    'sourceforge.net',
    'staticsdo.com',
    'stockstar.com',
    'sttlbb.com',
    'suning.com',
    'szhome.com',
    'sznews.com',
    'tangdou.com',
    'tanx.com',
    'tao123.com',
    'taobao.com',
    'taobaocdn.com',
    'tdimg.com',
    'tenpay.com',
    'tgbus.com',
    'theplanet.com',
    'thethirdmedia.com',
    'tiancity.com',
    'tianji.com',
    'tiao8.info',
    'tiexue.net',
    'titan24.com',
    'tmall.com',
    'tom.com',
    'toocle.com',
    'tremormedia.com',
    'tuan800.com',
    'tudou.com',
    'tudouui.com',
    'tui18.com',
    'tuniu.com',
    'twcczhu.com',
    'u17.com',
    'ucjoy.com',
    'ulink.cc',
    'uniontoufang.com',
    'up2c.com',
    'uuu9.com',
    'uuzu.com',
    'vancl.com',
    'verycd.com',
    'vipshop.com',
    'vizu.com',
    'vjia.com',
    'weibo.com',
    'weiphone.com',
    'west263.com',
    'whlongda.com',
    'wrating.com',
    'wumii.com',
    'xiami.com',
    'xiaomi.com',
    'xiazaiba.com',
    'xici.net',
    'xinhuanet.com',
    'xinnet.com',
    'xitek.com',
    'xiu.com',
    'xunlei.com',
    'xyxy.net',
    'yahoo.co.jp',
    'yahoo.com',
    'yaolan.com',
    'yesky.com',
    'yieldmanager.com',
    'yihaodian.com',
    'yingjiesheng.com',
    'yinyuetai.com',
    'yiqifa.com',
    'ykimg.com',
    'ynet.com',
    'yoka.com',
    'yolk7.com',
    'youboy.com',
    'youdao.com',
    'yougou.com',
    'youku.com',
    'youshang.com',
    'ytimg.com',
    'yupoo.com',
    'yxlady.com',
    'yyets.com',
    'zhaodao123.com',
    'zhaopin.com',
    'zhenai.com',
    'zhibo8.cc',
    'zhihu.com',
    'zhubajie.com',
    'zongheng.com',
    'zoosnet.net',
    'zqgame.com',
    'ztgame.com',
    'zx915.com'
]


def is_china_domain(domain):
    if domain.endswith('.cn'):
        return True
    for chain_domain in CHINA_DOMAINS:
        if domain == chain_domain or domain.endswith('.%s' % chain_domain):
            return True
    return False


def HOSTED_DOMAINS():
    return {
        # cdn
        'd2anp67vmqk4wc.cloudfront.net',
        # google.com
        'google.com', 'www.google.com',
        'mail.google.com', 'chatenabled.mail.google.com',
        'filetransferenabled.mail.google.com', 'apis.google.com',
        'mobile-gtalk.google.com', 'mtalk.google.com',
        # google.com.hk
        'google.com.hk', 'www.google.com.hk',
        # google.cn
        'google.cn', 'www.google.cn'
    }

# TODO cache
# TODO PTR support, check cache then check remote
# TODO IPV6
# TODO complete record types
# TODO --recursive

if '__main__' == __name__:
    main()