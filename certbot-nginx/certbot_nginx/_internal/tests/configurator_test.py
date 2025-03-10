"""Test for certbot_nginx._internal.configurator."""
import sys
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import serialization
import pytest

from acme import challenges
from acme import messages
from certbot import achallenges
from certbot import crypto_util
from certbot import errors
from certbot.compat import os
from certbot.tests import util as certbot_test_util
from certbot_nginx._internal import obj
from certbot_nginx._internal import parser
from certbot_nginx._internal.configurator import _redirect_block_for_domain
from certbot_nginx._internal.nginxparser import UnspacedList
from certbot_nginx._internal.tests import test_util as util


class NginxConfiguratorTest(util.NginxTest):
    """Test a semi complex vhost configuration."""

    def setUp(self):
        super().setUp()

        self.config = self.get_nginx_configurator(
            self.config_path, self.config_dir, self.work_dir, self.logs_dir)

        patch = mock.patch('certbot_nginx._internal.configurator.display_util.notify')
        self.mock_notify = patch.start()
        self.addCleanup(patch.stop)

    @mock.patch("certbot_nginx._internal.configurator.util.exe_exists")
    def test_prepare_no_install(self, mock_exe_exists):
        mock_exe_exists.return_value = False
        with pytest.raises(errors.NoInstallationError):
            self.config.prepare()

    def test_prepare(self):
        assert (1, 6, 2) == self.config.version
        assert 15 == len(self.config.parser.parsed)

    @mock.patch("certbot_nginx._internal.configurator.util.exe_exists")
    @mock.patch("certbot_nginx._internal.configurator.subprocess.run")
    def test_prepare_initializes_version(self, mock_run, mock_exe_exists):
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "\n".join(
                          ["nginx version: nginx/1.6.2",
                           "built by clang 6.0 (clang-600.0.56)"
                           " (based on LLVM 3.5svn)",
                           "TLS SNI support enabled",
                           "configure arguments: --prefix=/usr/local/Cellar/"
                           "nginx/1.6.2 --with-http_ssl_module"])

        mock_exe_exists.return_value = True

        self.config.version = None
        self.config.config_test = mock.Mock()
        self.config.prepare()
        assert (1, 6, 2) == self.config.version

    def test_prepare_locked(self):
        # It is important to test that server_root is locked during the call to
        # prepare (as opposed to somewhere else during plugin execution) to
        # ensure that this lock will be acquired after the Certbot package
        # acquires all of its locks. (Tests that Certbot calls prepare after
        # acquiring its locks are part of the Certbot package's tests.) Not
        # doing this could result in deadlock from two versions of Certbot that
        # acquire its locks in a different order.
        server_root = self.config.conf("server-root")

        from certbot import util as certbot_util
        certbot_util._LOCKS[server_root].release()  # pylint: disable=protected-access

        self.config.config_test = mock.Mock()
        certbot_test_util.lock_and_call(self._test_prepare_locked, server_root)

    @mock.patch("certbot_nginx._internal.configurator.util.exe_exists")
    def _test_prepare_locked(self, unused_exe_exists):
        try:
            self.config.prepare()
        except errors.PluginError as err:
            err_msg = str(err)
            assert "lock" in err_msg
            assert self.config.conf("server-root") in err_msg
        else:  # pragma: no cover
            self.fail("Exception wasn't raised!")

    @mock.patch("certbot_nginx._internal.configurator.socket.gethostname")
    @mock.patch("certbot_nginx._internal.configurator.socket.gethostbyaddr")
    def test_get_all_names(self, mock_gethostbyaddr, mock_gethostname):
        mock_gethostbyaddr.return_value = ('155.225.50.69.nephoscale.net', [], [])
        mock_gethostname.return_value = ('example.net')
        names = self.config.get_all_names()
        assert names == {
            "155.225.50.69.nephoscale.net", "www.example.org", "another.alias",
             "migration.com", "summer.com", "geese.com", "sslon.com",
             "globalssl.com", "globalsslsetssl.com", "ipv6.com", "ipv6ssl.com",
             "headers.com", "example.net", "ssl.both.com", 'addr-80.com'}

    def test_supported_enhancements(self):
        assert ['redirect', 'ensure-http-header', 'staple-ocsp'] == \
                         self.config.supported_enhancements()

    def test_enhance(self):
        with pytest.raises(errors.PluginError):
            self.config.enhance('myhost', 'unknown_enhancement')

    def test_get_chall_pref(self):
        assert [challenges.HTTP01] == \
                         self.config.get_chall_pref('myhost')

    def test_save(self):
        filep = self.config.parser.abs_path('sites-enabled/example.com')
        mock_vhost = obj.VirtualHost(filep,
                                     None, None, None,
                                     {'.example.com', 'example.*'},
                                     None, [0])
        self.config.parser.add_server_directives(
            mock_vhost,
            [['listen', ' ', '5001', ' ', 'ssl']])
        self.config.save()

        # pylint: disable=protected-access
        parsed = self.config.parser._parse_files(filep, override=True)
        assert [[['server'],
                           [['listen', '69.50.225.155:9000'],
                            ['listen', '127.0.0.1'],
                            ['server_name', '.example.com'],
                            ['server_name', 'example.*'],
                            ['listen', '5001', 'ssl'],
                            ['#', parser.COMMENT]]]] == \
                         parsed[filep]

    def test_choose_vhosts_alias(self):
        self._test_choose_vhosts_common('alias', 'server_conf')

    def test_choose_vhosts_example_com(self):
        self._test_choose_vhosts_common('example.com', 'example_conf')

    def test_choose_vhosts_localhost(self):
        self._test_choose_vhosts_common('localhost', 'localhost_conf')

    def test_choose_vhosts_example_com_uk_test(self):
        self._test_choose_vhosts_common('example.com.uk.test', 'example_conf')

    def test_choose_vhosts_www_example_com(self):
        self._test_choose_vhosts_common('www.example.com', 'example_conf')

    def test_choose_vhosts_test_www_example_com(self):
        self._test_choose_vhosts_common('test.www.example.com', 'foo_conf')

    def test_choose_vhosts_abc_www_foo_com(self):
        self._test_choose_vhosts_common('abc.www.foo.com', 'foo_conf')

    def test_choose_vhosts_www_bar_co_uk(self):
        self._test_choose_vhosts_common('www.bar.co.uk', 'localhost_conf')

    def test_choose_vhosts_ipv6_com(self):
        self._test_choose_vhosts_common('ipv6.com', 'ipv6_conf')

    def _test_choose_vhosts_common(self, name, conf):
        conf_names = {'localhost_conf': {'localhost', r'~^(www\.)?(example|bar)\.'},
                 'server_conf': {'somename', 'another.alias', 'alias'},
                 'example_conf': {'.example.com', 'example.*'},
                 'foo_conf': {'*.www.foo.com', '*.www.example.com'},
                 'ipv6_conf': {'ipv6.com'}}

        conf_path = {'localhost': "etc_nginx/nginx.conf",
                   'alias': "etc_nginx/nginx.conf",
                   'example.com': "etc_nginx/sites-enabled/example.com",
                   'example.com.uk.test': "etc_nginx/sites-enabled/example.com",
                   'www.example.com': "etc_nginx/sites-enabled/example.com",
                   'test.www.example.com': "etc_nginx/foo.conf",
                   'abc.www.foo.com': "etc_nginx/foo.conf",
                   'www.bar.co.uk': "etc_nginx/nginx.conf",
                   'ipv6.com': "etc_nginx/sites-enabled/ipv6.com"}
        conf_path = {key: os.path.normpath(value) for key, value in conf_path.items()}

        vhost = self.config.choose_vhosts(name)[0]
        path = os.path.relpath(vhost.filep, self.temp_dir)

        assert conf_names[conf] == vhost.names
        assert conf_path[name] == path
        # IPv6 specific checks
        if name == "ipv6.com":
            assert vhost.ipv6_enabled()
            # Make sure that we have SSL enabled also for IPv6 addr
            assert any(True for x in vhost.addrs if x.ssl and x.ipv6)

    def test_choose_vhosts_bad(self):
        bad_results = ['www.foo.com', 'example', 't.www.bar.co',
                       '69.255.225.155']

        for name in bad_results:
            with self.subTest(name=name):
                with pytest.raises(errors.MisconfigurationError):
                    self.config.choose_vhosts(name)

    def test_choose_vhosts_keep_ip_address(self):
        # no listen on port 80
        # listen       69.50.225.155:9000;
        # listen       127.0.0.1;
        vhost = self.config.choose_vhosts('example.com')[0]
        assert obj.Addr.fromstring("5001 ssl") in vhost.addrs

        # no listens at all
        vhost = self.config.choose_vhosts('headers.com')[0]
        assert obj.Addr.fromstring("5001 ssl") in vhost.addrs
        assert obj.Addr.fromstring("80") in vhost.addrs

        # blank addr listen on 80 should result in blank addr ssl
        # listen 80;
        # listen [::]:80;
        vhost = self.config.choose_vhosts('ipv6.com')[0]
        assert obj.Addr.fromstring("5001 ssl") in vhost.addrs
        assert obj.Addr.fromstring("[::]:5001 ssl") in vhost.addrs

        # listen on 80 with ip address should result in copied addr
        # listen 1.2.3.4:80;
        # listen [1:20::300]:80;
        vhost = self.config.choose_vhosts('addr-80.com')[0]
        assert obj.Addr.fromstring("1.2.3.4:5001 ssl") in vhost.addrs
        assert obj.Addr.fromstring("[1:20::300]:5001 ssl ipv6only=on") in vhost.addrs


    def test_ipv6only(self):
        # ipv6_info: (ipv6_active, ipv6only_present)
        assert (True, False) == self.config.ipv6_info("[::]", "80")
        # Port 443 has ipv6only=on because of ipv6ssl.com vhost
        assert (True, True) == self.config.ipv6_info("[::]", "443")

    def test_ipv6only_detection(self):
        self.config.version = (1, 3, 1)

        self.config.deploy_cert(
            "ipv6.com",
            "example/cert.pem",
            "example/key.pem",
            "example/chain.pem",
            "example/fullchain.pem")

        for addr in self.config.choose_vhosts("ipv6.com")[0].addrs:
            assert not addr.ipv6only

    def test_more_info(self):
        assert 'nginx.conf' in self.config.more_info()

    def test_deploy_cert_requires_fullchain_path(self):
        self.config.version = (1, 3, 1)
        with pytest.raises(errors.PluginError):
            self.config.deploy_cert("www.example.com",
            "example/cert.pem",
            "example/key.pem",
            "example/chain.pem",
            None)

    @mock.patch('certbot_nginx._internal.parser.NginxParser.update_or_add_server_directives')
    def test_deploy_cert_raise_on_add_error(self, mock_update_or_add_server_directives):
        mock_update_or_add_server_directives.side_effect = errors.MisconfigurationError()
        with pytest.raises(errors.PluginError):
            self.config.deploy_cert("migration.com",
            "example/cert.pem",
            "example/key.pem",
            "example/chain.pem",
            "example/fullchain.pem")

    def test_deploy_cert(self):
        server_conf = self.config.parser.abs_path('server.conf')
        nginx_conf = self.config.parser.abs_path('nginx.conf')
        example_conf = self.config.parser.abs_path('sites-enabled/example.com')
        self.config.version = (1, 3, 1)

        # Get the default SSL vhost
        self.config.deploy_cert(
            "www.example.com",
            "example/cert.pem",
            "example/key.pem",
            "example/chain.pem",
            "example/fullchain.pem")
        self.config.deploy_cert(
            "another.alias",
            "/etc/nginx/cert.pem",
            "/etc/nginx/key.pem",
            "/etc/nginx/chain.pem",
            "/etc/nginx/fullchain.pem")
        self.config.save()

        self.config.parser.load()

        parsed_example_conf = util.filter_comments(self.config.parser.parsed[example_conf])
        parsed_server_conf = util.filter_comments(self.config.parser.parsed[server_conf])
        parsed_nginx_conf = util.filter_comments(self.config.parser.parsed[nginx_conf])

        assert [[['server'],
                           [
                            ['listen', '69.50.225.155:9000'],
                            ['listen', '127.0.0.1'],
                            ['server_name', '.example.com'],
                            ['server_name', 'example.*'],

                            ['listen', '5001', 'ssl'],
                            ['ssl_certificate', 'example/fullchain.pem'],
                            ['ssl_certificate_key', 'example/key.pem'],
                            ['include', self.config.mod_ssl_conf],
                            ['ssl_dhparam', self.config.ssl_dhparams],
                            ]]] == \
                         parsed_example_conf
        assert [['server_name', 'somename', 'alias', 'another.alias']] == \
                         parsed_server_conf
        assert util.contains_at_depth(
            parsed_nginx_conf,
            [['server'],
             [
              ['listen', '8000'],
              ['listen', 'somename:8080'],
              ['include', 'server.conf'],
              [['location', '/'],
               [['root', 'html'],
                ['index', 'index.html', 'index.htm']]],
              ['listen', '5001', 'ssl'],
              ['ssl_certificate', '/etc/nginx/fullchain.pem'],
              ['ssl_certificate_key', '/etc/nginx/key.pem'],
              ['include', self.config.mod_ssl_conf],
              ['ssl_dhparam', self.config.ssl_dhparams],
            ]],
            2)

    def test_deploy_cert_add_explicit_listen(self):
        migration_conf = self.config.parser.abs_path('sites-enabled/migration.com')
        self.config.deploy_cert(
            "summer.com",
            "summer/cert.pem",
            "summer/key.pem",
            "summer/chain.pem",
            "summer/fullchain.pem")
        self.config.save()
        self.config.parser.load()
        parsed_migration_conf = util.filter_comments(self.config.parser.parsed[migration_conf])
        assert [['server'],
                          [
                           ['server_name', 'migration.com'],
                           ['server_name', 'summer.com'],

                           ['listen', '80'],
                           ['listen', '5001', 'ssl'],
                           ['ssl_certificate', 'summer/fullchain.pem'],
                           ['ssl_certificate_key', 'summer/key.pem'],
                           ['include', self.config.mod_ssl_conf],
                           ['ssl_dhparam', self.config.ssl_dhparams],
                           ]] == \
                         parsed_migration_conf[0]

    @mock.patch("certbot_nginx._internal.configurator.http_01.NginxHttp01.perform")
    @mock.patch("certbot_nginx._internal.configurator.NginxConfigurator.restart")
    @mock.patch("certbot_nginx._internal.configurator.NginxConfigurator.revert_challenge_config")
    def test_perform_and_cleanup(self, mock_revert, mock_restart, mock_http_perform):
        # Only tests functionality specific to configurator.perform
        # Note: As more challenges are offered this will have to be expanded
        achall = achallenges.KeyAuthorizationAnnotatedChallenge(
            challb=messages.ChallengeBody(
                chall=challenges.HTTP01(token=b"m8TdO1qik4JVFtgPPurJmg"),
                uri="https://ca.org/chall1_uri",
                status=messages.Status("pending"),
            ), domain="example.com", account_key=self.rsa512jwk)

        expected = [
            achall.response(self.rsa512jwk),
        ]

        mock_http_perform.return_value = expected[:]
        responses = self.config.perform([achall])

        assert mock_http_perform.call_count == 1
        assert responses == expected

        self.config.cleanup([achall])
        assert 0 == self.config._chall_out # pylint: disable=protected-access
        assert mock_revert.call_count == 1
        assert mock_restart.call_count == 2

    @mock.patch("certbot_nginx._internal.configurator.subprocess.run")
    def test_get_version(self, mock_run):
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "\n".join(
                          ["nginx version: nginx/1.4.2",
                           "built by clang 6.0 (clang-600.0.56)"
                           " (based on LLVM 3.5svn)",
                           "TLS SNI support enabled",
                           "configure arguments: --prefix=/usr/local/Cellar/"
                           "nginx/1.6.2 --with-http_ssl_module"])
        assert self.config.get_version() == (1, 4, 2)

        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "\n".join(
                          ["nginx version: nginx/0.9",
                           "built by clang 6.0 (clang-600.0.56)"
                           " (based on LLVM 3.5svn)",
                           "TLS SNI support enabled",
                           "configure arguments: --with-http_ssl_module"])
        assert self.config.get_version() == (0, 9)

        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "\n".join(
                          ["blah 0.0.1",
                           "built by clang 6.0 (clang-600.0.56)"
                           " (based on LLVM 3.5svn)",
                           "TLS SNI support enabled",
                           "configure arguments: --with-http_ssl_module"])
        with pytest.raises(errors.PluginError):
            self.config.get_version()

        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "\n".join(
                          ["nginx version: nginx/1.4.2",
                           "TLS SNI support enabled"])
        with pytest.raises(errors.PluginError):
            self.config.get_version()

        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "\n".join(
                          ["nginx version: nginx/1.4.2",
                           "built by clang 6.0 (clang-600.0.56)"
                           " (based on LLVM 3.5svn)",
                           "configure arguments: --with-http_ssl_module"])
        with pytest.raises(errors.PluginError):
            self.config.get_version()

        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "\n".join(
                          ["nginx version: nginx/0.8.1",
                           "built by clang 6.0 (clang-600.0.56)"
                           " (based on LLVM 3.5svn)",
                           "TLS SNI support enabled",
                           "configure arguments: --with-http_ssl_module"])
        with pytest.raises(errors.NotSupportedError):
            self.config.get_version()

        mock_run.side_effect = OSError("Can't find program")
        with pytest.raises(errors.PluginError):
            self.config.get_version()

    @mock.patch("certbot_nginx._internal.configurator.subprocess.run")
    def test_get_openssl_version(self, mock_run):
        # pylint: disable=protected-access
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = """
                nginx version: nginx/1.15.5
                built by gcc 5.4.0 20160609 (Ubuntu 5.4.0-6ubuntu1~16.04.9)
                built with OpenSSL 1.0.2g  1 Mar 2016
                TLS SNI support enabled
                configure arguments:
            """
        assert self.config._get_openssl_version() == "1.0.2g"

        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = """
                nginx version: nginx/1.15.5
                built by gcc 5.4.0 20160609 (Ubuntu 5.4.0-6ubuntu1~16.04.9)
                built with OpenSSL 1.0.2-beta1  1 Mar 2016
                TLS SNI support enabled
                configure arguments:
            """
        assert self.config._get_openssl_version() == "1.0.2-beta1"

        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = """
                nginx version: nginx/1.15.5
                built by gcc 5.4.0 20160609 (Ubuntu 5.4.0-6ubuntu1~16.04.9)
                built with OpenSSL 1.0.2  1 Mar 2016
                TLS SNI support enabled
                configure arguments:
            """
        assert self.config._get_openssl_version() == "1.0.2"

        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = """
                nginx version: nginx/1.15.5
                built by gcc 5.4.0 20160609 (Ubuntu 5.4.0-6ubuntu1~16.04.9)
                built with OpenSSL 1.0.2g  1 Mar 2016 (running with OpenSSL 1.0.2a  1 Mar 2016)
                TLS SNI support enabled
                configure arguments:
            """
        assert self.config._get_openssl_version() == "1.0.2a"

        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = """
                nginx version: nginx/1.15.5
                built by gcc 5.4.0 20160609 (Ubuntu 5.4.0-6ubuntu1~16.04.9)
                built with LibreSSL 2.2.2
                TLS SNI support enabled
                configure arguments:
            """
        assert self.config._get_openssl_version() == ""

        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = """
                nginx version: nginx/1.15.5
                built by gcc 5.4.0 20160609 (Ubuntu 5.4.0-6ubuntu1~16.04.9)
                TLS SNI support enabled
                configure arguments:
            """
        assert self.config._get_openssl_version() == ""

    @mock.patch("certbot_nginx._internal.configurator.subprocess.run")
    @mock.patch("certbot_nginx._internal.configurator.time")
    def test_nginx_restart(self, mock_time, mock_run):
        mocked = mock_run.return_value
        mocked.stdout = ''
        mocked.stderr = ''
        mocked.returncode = 0
        self.config.restart()
        assert mock_run.call_count == 1
        mock_time.sleep.assert_called_once_with(0.1234)

    @mock.patch("certbot_nginx._internal.configurator.subprocess.run")
    @mock.patch("certbot_nginx._internal.configurator.logger.debug")
    def test_nginx_restart_fail(self, mock_log_debug, mock_run):
        mocked = mock_run.return_value
        mocked.stdout = ''
        mocked.stderr = ''
        mocked.returncode = 1
        with pytest.raises(errors.MisconfigurationError):
            self.config.restart()
        assert mock_run.call_count == 2
        mock_log_debug.assert_called_once_with("nginx reload failed:\n%s", "")

    @mock.patch("certbot_nginx._internal.configurator.subprocess.run")
    def test_no_nginx_start(self, mock_run):
        mock_run.side_effect = OSError("Can't find program")
        with pytest.raises(errors.MisconfigurationError):
            self.config.restart()

    @mock.patch("certbot.util.run_script")
    def test_config_test_bad_process(self, mock_run_script):
        mock_run_script.side_effect = errors.SubprocessError
        with pytest.raises(errors.MisconfigurationError):
            self.config.config_test()

    @mock.patch("certbot.util.run_script")
    def test_config_test(self, _):
        self.config.config_test()

    @mock.patch("certbot.reverter.Reverter.recovery_routine")
    def test_recovery_routine_throws_error_from_reverter(self, mock_recovery_routine):
        mock_recovery_routine.side_effect = errors.ReverterError("foo")
        with pytest.raises(errors.PluginError):
            self.config.recovery_routine()

    @mock.patch("certbot.reverter.Reverter.rollback_checkpoints")
    def test_rollback_checkpoints_throws_error_from_reverter(self, mock_rollback_checkpoints):
        mock_rollback_checkpoints.side_effect = errors.ReverterError("foo")
        with pytest.raises(errors.PluginError):
            self.config.rollback_checkpoints()

    @mock.patch("certbot.reverter.Reverter.revert_temporary_config")
    def test_revert_challenge_config_throws_error_from_reverter(self, mock_revert_temporary_config):
        mock_revert_temporary_config.side_effect = errors.ReverterError("foo")
        with pytest.raises(errors.PluginError):
            self.config.revert_challenge_config()

    @mock.patch("certbot.reverter.Reverter.add_to_checkpoint")
    def test_save_throws_error_from_reverter(self, mock_add_to_checkpoint):
        mock_add_to_checkpoint.side_effect = errors.ReverterError("foo")
        with pytest.raises(errors.PluginError):
            self.config.save()

    def test_get_snakeoil_paths(self):
        # pylint: disable=protected-access
        cert, key = self.config._get_snakeoil_paths()
        assert os.path.exists(cert)
        assert os.path.exists(key)
        with open(cert, "rb") as cert_file:
            x509.load_pem_x509_certificate(cert_file.read())
        with open(key, "rb") as key_file:
            serialization.load_pem_private_key(key_file.read(), password=None)

    def test_redirect_enhance(self):
        # Test that we successfully add a redirect when there is
        # a listen directive
        expected = UnspacedList(_redirect_block_for_domain("www.example.com"))[0]

        example_conf = self.config.parser.abs_path('sites-enabled/example.com')
        self.config.enhance("www.example.com", "redirect")

        generated_conf = self.config.parser.parsed[example_conf]
        assert util.contains_at_depth(generated_conf, expected, 2) is True

        # Test that we successfully add a redirect when there is
        # no listen directive
        migration_conf = self.config.parser.abs_path('sites-enabled/migration.com')
        self.config.enhance("migration.com", "redirect")

        expected = UnspacedList(_redirect_block_for_domain("migration.com"))[0]

        generated_conf = self.config.parser.parsed[migration_conf]
        assert util.contains_at_depth(generated_conf, expected, 2) is True

    def test_split_for_redirect(self):
        example_conf = self.config.parser.abs_path('sites-enabled/example.com')
        self.config.deploy_cert(
            "example.org",
            "example/cert.pem",
            "example/key.pem",
            "example/chain.pem",
            "example/fullchain.pem")
        self.config.enhance("www.example.com", "redirect")
        generated_conf = self.config.parser.parsed[example_conf]
        assert [[['server'], [
               ['server_name', '.example.com'],
               ['server_name', 'example.*'],
               ['listen', '5001', 'ssl'], ['#', ' managed by Certbot'],
               ['ssl_certificate', 'example/fullchain.pem'], ['#', ' managed by Certbot'],
               ['ssl_certificate_key', 'example/key.pem'], ['#', ' managed by Certbot'],
               ['include', self.config.mod_ssl_conf], ['#', ' managed by Certbot'],
               ['ssl_dhparam', self.config.ssl_dhparams], ['#', ' managed by Certbot'],
               [], []]],
             [['server'], [
               [['if', '($host', '=', 'www.example.com)'], [
                 ['return', '301', 'https://$host$request_uri']]],
               ['#', ' managed by Certbot'], [],
               ['listen', '69.50.225.155:9000'],
               ['listen', '127.0.0.1'],
               ['server_name', '.example.com'],
               ['server_name', 'example.*'],
               ['return', '404'], ['#', ' managed by Certbot'], [], [], []]]] == \
            generated_conf

    def test_split_for_headers(self):
        example_conf = self.config.parser.abs_path('sites-enabled/example.com')
        self.config.deploy_cert(
            "example.org",
            "example/cert.pem",
            "example/key.pem",
            "example/chain.pem",
            "example/fullchain.pem")
        self.config.enhance("www.example.com", "ensure-http-header", "Strict-Transport-Security")
        generated_conf = self.config.parser.parsed[example_conf]
        assert [[['server'], [
               ['server_name', '.example.com'],
               ['server_name', 'example.*'],
               ['listen', '5001', 'ssl'], ['#', ' managed by Certbot'],
               ['ssl_certificate', 'example/fullchain.pem'], ['#', ' managed by Certbot'],
               ['ssl_certificate_key', 'example/key.pem'], ['#', ' managed by Certbot'],
               ['include', self.config.mod_ssl_conf], ['#', ' managed by Certbot'],
               ['ssl_dhparam', self.config.ssl_dhparams], ['#', ' managed by Certbot'],
               [], [],
               ['add_header', 'Strict-Transport-Security', '"max-age=31536000"', 'always'],
               ['#', ' managed by Certbot'],
               [], []]],
             [['server'], [
               ['listen', '69.50.225.155:9000'],
               ['listen', '127.0.0.1'],
               ['server_name', '.example.com'],
               ['server_name', 'example.*'],
               [], []]]] == \
            generated_conf

    def test_http_header_hsts(self):
        example_conf = self.config.parser.abs_path('sites-enabled/example.com')
        self.config.enhance("www.example.com", "ensure-http-header",
                            "Strict-Transport-Security")
        expected = ['add_header', 'Strict-Transport-Security', '"max-age=31536000"', 'always']
        generated_conf = self.config.parser.parsed[example_conf]
        assert util.contains_at_depth(generated_conf, expected, 2) is True

    def test_multiple_headers_hsts(self):
        headers_conf = self.config.parser.abs_path('sites-enabled/headers.com')
        self.config.enhance("headers.com", "ensure-http-header",
                            "Strict-Transport-Security")
        expected = ['add_header', 'Strict-Transport-Security', '"max-age=31536000"', 'always']
        generated_conf = self.config.parser.parsed[headers_conf]
        assert util.contains_at_depth(generated_conf, expected, 2) is True

    def test_http_header_hsts_twice(self):
        self.config.enhance("www.example.com", "ensure-http-header",
                            "Strict-Transport-Security")
        with pytest.raises(errors.PluginEnhancementAlreadyPresent):
            self.config.enhance("www.example.com",
            "ensure-http-header", "Strict-Transport-Security")

    @mock.patch('certbot_nginx._internal.obj.VirtualHost.contains_list')
    def test_certbot_redirect_exists(self, mock_contains_list):
        # Test that we add no redirect statement if there is already a
        # redirect in the block that is managed by certbot
        # Has a certbot redirect
        mock_contains_list.return_value = True
        with mock.patch("certbot_nginx._internal.configurator.logger") as mock_logger:
            self.config.enhance("www.example.com", "redirect")
            assert mock_logger.info.call_args[0][0] == \
                "Traffic on port %s already redirecting to ssl in %s"

    def test_redirect_dont_enhance(self):
        # Test that we don't accidentally add redirect to ssl-only block
        with mock.patch("certbot_nginx._internal.configurator.logger") as mock_logger:
            self.config.enhance("geese.com", "redirect")
        assert mock_logger.info.call_args[0][0] == \
                'No matching insecure server blocks listening on port %s found.'

    def test_double_redirect(self):
        # Test that we add one redirect for each domain
        example_conf = self.config.parser.abs_path('sites-enabled/example.com')
        self.config.enhance("example.com", "redirect")
        self.config.enhance("example.org", "redirect")

        expected1 = UnspacedList(_redirect_block_for_domain("example.com"))[0]
        expected2 = UnspacedList(_redirect_block_for_domain("example.org"))[0]

        generated_conf = self.config.parser.parsed[example_conf]
        assert util.contains_at_depth(generated_conf, expected1, 2)
        assert util.contains_at_depth(generated_conf, expected2, 2)

    def test_staple_ocsp_bad_version(self):
        self.config.version = (1, 3, 1)
        with pytest.raises(errors.PluginError):
            self.config.enhance("www.example.com", "staple-ocsp", "chain_path")

    def test_staple_ocsp_no_chain_path(self):
        with pytest.raises(errors.PluginError):
            self.config.enhance("www.example.com", "staple-ocsp", None)

    def test_staple_ocsp_internal_error(self):
        self.config.enhance("www.example.com", "staple-ocsp", "chain_path")
        # error is raised because the server block has conflicting directives
        with pytest.raises(errors.PluginError):
            self.config.enhance("www.example.com", "staple-ocsp", "different_path")

    def test_staple_ocsp(self):
        chain_path = "example/chain.pem"
        self.config.enhance("www.example.com", "staple-ocsp", chain_path)

        example_conf = self.config.parser.abs_path('sites-enabled/example.com')
        generated_conf = self.config.parser.parsed[example_conf]

        assert util.contains_at_depth(
            generated_conf,
            ['ssl_trusted_certificate', 'example/chain.pem'], 2)
        assert util.contains_at_depth(
            generated_conf, ['ssl_stapling', 'on'], 2)
        assert util.contains_at_depth(
            generated_conf, ['ssl_stapling_verify', 'on'], 2)

    def test_deploy_no_match_default_set(self):
        default_conf = self.config.parser.abs_path('sites-enabled/default')
        foo_conf = self.config.parser.abs_path('foo.conf')
        del self.config.parser.parsed[foo_conf][2][1][0][1][0] # remove default_server
        self.config.version = (1, 3, 1)

        self.config.deploy_cert(
            "www.nomatch.com",
            "example/cert.pem",
            "example/key.pem",
            "example/chain.pem",
            "example/fullchain.pem")
        self.config.save()

        self.config.parser.load()

        parsed_default_conf = util.filter_comments(self.config.parser.parsed[default_conf])

        assert [[['server'],
                           [['listen', 'myhost', 'default_server'],
                            ['listen', 'otherhost', 'default_server'],
                            ['server_name', '"www.example.org"'],
                            [['location', '/'],
                             [['root', 'html'],
                              ['index', 'index.html', 'index.htm']]]]],
                          [['server'],
                           [['listen', 'myhost'],
                            ['listen', 'otherhost'],
                            ['server_name', 'www.nomatch.com'],
                            [['location', '/'],
                             [['root', 'html'],
                              ['index', 'index.html', 'index.htm']]],
                            ['listen', '5001', 'ssl'],
                            ['ssl_certificate', 'example/fullchain.pem'],
                            ['ssl_certificate_key', 'example/key.pem'],
                            ['include', self.config.mod_ssl_conf],
                            ['ssl_dhparam', self.config.ssl_dhparams]]]] == \
                         parsed_default_conf

        self.config.deploy_cert(
            "nomatch.com",
            "example/cert.pem",
            "example/key.pem",
            "example/chain.pem",
            "example/fullchain.pem")
        self.config.save()

        self.config.parser.load()

        parsed_default_conf = util.filter_comments(self.config.parser.parsed[default_conf])

        assert util.contains_at_depth(parsed_default_conf, "nomatch.com", 3)

    def test_deploy_no_match_default_set_multi_level_path(self):
        default_conf = self.config.parser.abs_path('sites-enabled/default')
        foo_conf = self.config.parser.abs_path('foo.conf')
        del self.config.parser.parsed[default_conf][0][1][0]
        del self.config.parser.parsed[default_conf][0][1][0]
        self.config.version = (1, 3, 1)

        self.config.deploy_cert(
            "www.nomatch.com",
            "example/cert.pem",
            "example/key.pem",
            "example/chain.pem",
            "example/fullchain.pem")
        self.config.save()

        self.config.parser.load()

        parsed_foo_conf = util.filter_comments(self.config.parser.parsed[foo_conf])

        assert [['server'],
                          [['listen', '*:80', 'ssl'],
                          ['server_name', 'www.nomatch.com'],
                          ['root', '/home/ubuntu/sites/foo/'],
                          [['location', '/status'], [[['types'], [['image/jpeg', 'jpg']]]]],
                          [['location', '~', 'case_sensitive\\.php$'], [['index', 'index.php'],
                           ['root', '/var/root']]],
                          [['location', '~*', 'case_insensitive\\.php$'], []],
                          [['location', '=', 'exact_match\\.php$'], []],
                          [['location', '^~', 'ignore_regex\\.php$'], []],
                          ['ssl_certificate', 'example/fullchain.pem'],
                          ['ssl_certificate_key', 'example/key.pem']]] == \
                         parsed_foo_conf[1][1][1]

    def test_deploy_no_match_no_default_set(self):
        default_conf = self.config.parser.abs_path('sites-enabled/default')
        foo_conf = self.config.parser.abs_path('foo.conf')
        del self.config.parser.parsed[default_conf][0][1][0]
        del self.config.parser.parsed[default_conf][0][1][0]
        del self.config.parser.parsed[foo_conf][2][1][0][1][0]
        self.config.version = (1, 3, 1)

        with pytest.raises(errors.MisconfigurationError):
            self.config.deploy_cert("www.nomatch.com", "example/cert.pem", "example/key.pem",
            "example/chain.pem", "example/fullchain.pem")

    def test_deploy_no_match_fail_multiple_defaults(self):
        self.config.version = (1, 3, 1)
        with pytest.raises(errors.MisconfigurationError):
            self.config.deploy_cert("www.nomatch.com", "example/cert.pem", "example/key.pem",
            "example/chain.pem", "example/fullchain.pem")

    def test_deploy_no_match_multiple_defaults_ok(self):
        foo_conf = self.config.parser.abs_path('foo.conf')
        self.config.parser.parsed[foo_conf][2][1][0][1][0][1] = '*:5001'
        self.config.version = (1, 3, 1)
        self.config.deploy_cert("www.nomatch.com", "example/cert.pem", "example/key.pem",
            "example/chain.pem", "example/fullchain.pem")

    def test_deploy_no_match_add_redirect(self):
        default_conf = self.config.parser.abs_path('sites-enabled/default')
        foo_conf = self.config.parser.abs_path('foo.conf')
        del self.config.parser.parsed[foo_conf][2][1][0][1][0] # remove default_server
        self.config.version = (1, 3, 1)

        self.config.deploy_cert(
            "www.nomatch.com",
            "example/cert.pem",
            "example/key.pem",
            "example/chain.pem",
            "example/fullchain.pem")

        self.config.deploy_cert(
            "nomatch.com",
            "example/cert.pem",
            "example/key.pem",
            "example/chain.pem",
            "example/fullchain.pem")

        self.config.enhance("www.nomatch.com", "redirect")

        self.config.save()

        self.config.parser.load()

        expected = UnspacedList(_redirect_block_for_domain("www.nomatch.com"))[0]

        generated_conf = self.config.parser.parsed[default_conf]
        assert util.contains_at_depth(generated_conf, expected, 2)

    @mock.patch('certbot.reverter.logger')
    @mock.patch('certbot_nginx._internal.parser.NginxParser.load')
    def test_parser_reload_after_config_changes(self, mock_parser_load, unused_mock_logger):
        self.config.recovery_routine()
        self.config.revert_challenge_config()
        self.config.rollback_checkpoints()
        assert mock_parser_load.call_count == 3

    def test_choose_vhosts_wildcard(self):
        # pylint: disable=protected-access
        mock_path = "certbot_nginx._internal.display_ops.select_vhost_multiple"
        with mock.patch(mock_path) as mock_select_vhs:
            vhost = [x for x in self.config.parser.get_vhosts()
              if 'summer.com' in x.names][0]
            mock_select_vhs.return_value = [vhost]
            vhs = self.config._choose_vhosts_wildcard("*.com",
                                                     prefer_ssl=True)
            # Check that the dialog was called with migration.com
            assert vhost in mock_select_vhs.call_args[0][0]

            # And the actual returned values
            assert len(vhs) == 1
            assert vhs[0] == vhost

    def test_choose_vhosts_wildcard_redirect(self):
        # pylint: disable=protected-access
        mock_path = "certbot_nginx._internal.display_ops.select_vhost_multiple"
        with mock.patch(mock_path) as mock_select_vhs:
            vhost = [x for x in self.config.parser.get_vhosts()
              if 'summer.com' in x.names][0]
            mock_select_vhs.return_value = [vhost]
            vhs = self.config._choose_vhosts_wildcard("*.com",
                                                     prefer_ssl=False)
            # Check that the dialog was called with migration.com
            assert vhost in mock_select_vhs.call_args[0][0]

            # And the actual returned values
            assert len(vhs) == 1
            assert vhs[0] == vhost

    def test_deploy_cert_wildcard(self):
        # pylint: disable=protected-access
        mock_choose_vhosts = mock.MagicMock()
        vhost = [x for x in self.config.parser.get_vhosts()
            if 'geese.com' in x.names][0]
        mock_choose_vhosts.return_value = [vhost]
        self.config._choose_vhosts_wildcard = mock_choose_vhosts
        mock_d = "certbot_nginx._internal.configurator.NginxConfigurator._deploy_cert"
        with mock.patch(mock_d) as mock_dep:
            self.config.deploy_cert("*.com", "/tmp/path",
                                    "/tmp/path", "/tmp/path", "/tmp/path")
            assert mock_dep.called
            assert len(mock_dep.call_args_list) == 1
            assert vhost == mock_dep.call_args_list[0][0][0]

    @mock.patch("certbot_nginx._internal.display_ops.select_vhost_multiple")
    def test_deploy_cert_wildcard_no_vhosts(self, mock_dialog):
        # pylint: disable=protected-access
        mock_dialog.return_value = []
        with pytest.raises(errors.PluginError):
            self.config.deploy_cert("*.wild.cat", "/tmp/path", "/tmp/path",
                           "/tmp/path", "/tmp/path")

    @mock.patch("certbot_nginx._internal.display_ops.select_vhost_multiple")
    def test_enhance_wildcard_ocsp_after_install(self, mock_dialog):
        # pylint: disable=protected-access
        vhost = [x for x in self.config.parser.get_vhosts()
            if 'geese.com' in x.names][0]
        self.config._wildcard_vhosts["*.com"] = [vhost]
        self.config.enhance("*.com", "staple-ocsp", "example/chain.pem")
        assert not mock_dialog.called

    @mock.patch("certbot_nginx._internal.display_ops.select_vhost_multiple")
    def test_enhance_wildcard_redirect_or_ocsp_no_install(self, mock_dialog):
        vhost = [x for x in self.config.parser.get_vhosts()
            if 'summer.com' in x.names][0]
        mock_dialog.return_value = [vhost]
        self.config.enhance("*.com", "staple-ocsp", "example/chain.pem")
        assert mock_dialog.called is True

    @mock.patch("certbot_nginx._internal.display_ops.select_vhost_multiple")
    def test_enhance_wildcard_double_redirect(self, mock_dialog):
      # pylint: disable=protected-access
        vhost = [x for x in self.config.parser.get_vhosts()
            if 'summer.com' in x.names][0]
        self.config._wildcard_redirect_vhosts["*.com"] = [vhost]
        self.config.enhance("*.com", "redirect")
        assert not mock_dialog.called

    def test_choose_vhosts_wildcard_no_ssl_filter_port(self):
        # pylint: disable=protected-access
        mock_path = "certbot_nginx._internal.display_ops.select_vhost_multiple"
        with mock.patch(mock_path) as mock_select_vhs:
            mock_select_vhs.return_value = []
            self.config._choose_vhosts_wildcard("*.com",
                                                prefer_ssl=False,
                                                no_ssl_filter_port='80')
            # Check that the dialog was called with only port 80 vhosts
            assert len(mock_select_vhs.call_args[0][0]) == 9

    def test_choose_auth_vhosts(self):
        """choose_auth_vhosts correctly selects duplicative and HTTP/HTTPS vhosts"""
        http, https = self.config.choose_auth_vhosts('ssl.both.com')
        assert len(http) == 4
        assert len(https) == 2
        assert http[0].names == {'ssl.both.com'}
        assert http[1].names == {'ssl.both.com'}
        assert http[2].names == {'ssl.both.com'}
        assert http[3].names == {'*.both.com'}
        assert https[0].names == {'ssl.both.com'}
        assert https[1].names == {'*.both.com'}


class InstallSslOptionsConfTest(util.NginxTest):
    """Test that the options-ssl-nginx.conf file is installed and updated properly."""

    def setUp(self):
        super().setUp()

        self.config = self.get_nginx_configurator(
            self.config_path, self.config_dir, self.work_dir, self.logs_dir)

    def _call(self):
        self.config.install_ssl_options_conf(self.config.mod_ssl_conf,
            self.config.updated_mod_ssl_conf_digest)

    def _current_ssl_options_hash(self):
        return crypto_util.sha256sum(self.config.mod_ssl_conf_src)

    def _assert_current_file(self):
        assert os.path.isfile(self.config.mod_ssl_conf)
        assert crypto_util.sha256sum(self.config.mod_ssl_conf) == \
            self._current_ssl_options_hash()

    def test_no_file(self):
        # prepare should have placed a file there
        self._assert_current_file()
        os.remove(self.config.mod_ssl_conf)
        assert not os.path.isfile(self.config.mod_ssl_conf)
        self._call()
        self._assert_current_file()

    def test_current_file(self):
        self._assert_current_file()
        self._call()
        self._assert_current_file()

    def _mock_hash_except_ssl_conf_src(self, fake_hash):
        # Write a bad file in place so that update tests fail if no update occurs.
        # We're going to pretend this file (the currently installed conf file)
        # actually hashes to `fake_hash` for the update tests.
        with open(self.config.mod_ssl_conf, "w") as f:
            f.write("bogus")
        sha256 = crypto_util.sha256sum
        def _hash(filename):
            return sha256(filename) if filename == self.config.mod_ssl_conf_src else fake_hash
        return _hash

    def test_prev_file_updates_to_current(self):
        from certbot_nginx._internal.constants import ALL_SSL_OPTIONS_HASHES
        with mock.patch('certbot.crypto_util.sha256sum',
                new=self._mock_hash_except_ssl_conf_src(ALL_SSL_OPTIONS_HASHES[0])):
            self._call()
        self._assert_current_file()

    def test_prev_file_updates_to_current_old_nginx(self):
        from certbot_nginx._internal.constants import ALL_SSL_OPTIONS_HASHES
        self.config.version = (1, 5, 8)
        with mock.patch('certbot.crypto_util.sha256sum',
                new=self._mock_hash_except_ssl_conf_src(ALL_SSL_OPTIONS_HASHES[0])):
            self._call()
        self._assert_current_file()

    def test_manually_modified_current_file_does_not_update(self):
        with open(self.config.mod_ssl_conf, "a") as mod_ssl_conf:
            mod_ssl_conf.write("a new line for the wrong hash\n")
        with mock.patch("certbot.plugins.common.logger") as mock_logger:
            self._call()
            assert not mock_logger.warning.called
        assert os.path.isfile(self.config.mod_ssl_conf)
        assert crypto_util.sha256sum(self.config.mod_ssl_conf_src) == \
            self._current_ssl_options_hash()
        assert crypto_util.sha256sum(self.config.mod_ssl_conf) != \
            self._current_ssl_options_hash()

    def test_manually_modified_past_file_warns(self):
        with open(self.config.mod_ssl_conf, "a") as mod_ssl_conf:
            mod_ssl_conf.write("a new line for the wrong hash\n")
        with open(self.config.updated_mod_ssl_conf_digest, "w") as f:
            f.write("hashofanoldversion")
        with mock.patch("certbot.plugins.common.logger") as mock_logger:
            self._call()
            assert mock_logger.warning.call_args[0][0] == \
                "%s has been manually modified; updated file " \
                "saved to %s. We recommend updating %s for security purposes."
        assert crypto_util.sha256sum(self.config.mod_ssl_conf_src) == \
            self._current_ssl_options_hash()
        # only print warning once
        with mock.patch("certbot.plugins.common.logger") as mock_logger:
            self._call()
            assert not mock_logger.warning.called

    def test_current_file_hash_in_all_hashes(self):
        from certbot_nginx._internal.constants import ALL_SSL_OPTIONS_HASHES
        assert self._current_ssl_options_hash() in ALL_SSL_OPTIONS_HASHES, \
            "Constants.ALL_SSL_OPTIONS_HASHES must be appended" \
            " with the sha256 hash of self.config.mod_ssl_conf when it is updated."

    def test_ssl_config_files_hash_in_all_hashes(self):
        """
        It is really critical that all TLS Nginx config files have their SHA256 hash registered in
        constants.ALL_SSL_OPTIONS_HASHES. Otherwise Certbot will mistakenly assume that the config
        file has been manually edited by the user, and will refuse to update it.
        This test ensures that all necessary hashes are present.
        """
        import importlib.resources

        from certbot_nginx._internal.constants import ALL_SSL_OPTIONS_HASHES

        tls_configs_ref = importlib.resources.files("certbot_nginx").joinpath(
            "_internal", "tls_configs")
        with importlib.resources.as_file(tls_configs_ref) as tls_configs_dir:
            for tls_config_file in os.listdir(tls_configs_dir):
                file_hash = crypto_util.sha256sum(os.path.join(tls_configs_dir, tls_config_file))
                assert file_hash in ALL_SSL_OPTIONS_HASHES, \
                    f"Constants.ALL_SSL_OPTIONS_HASHES must be appended with the sha256 " \
                    f"hash of {tls_config_file} when it is updated."

    def test_nginx_version_uses_correct_config(self):
        self.config.version = (1, 5, 8)
        self.config.openssl_version = "1.0.2g" # shouldn't matter
        assert os.path.basename(self.config.mod_ssl_conf_src) == \
                         "options-ssl-nginx-old.conf"
        self._call()
        self._assert_current_file()
        self.config.version = (1, 5, 9)
        self.config.openssl_version = "1.0.2l"
        assert os.path.basename(self.config.mod_ssl_conf_src) == \
                         "options-ssl-nginx-tls12-only.conf"
        self._call()
        self._assert_current_file()
        self.config.version = (1, 13, 0)
        assert os.path.basename(self.config.mod_ssl_conf_src) == \
                         "options-ssl-nginx.conf"
        self._call()
        self._assert_current_file()
        self.config.version = (1, 13, 0)
        self.config.openssl_version = "1.0.2k"
        assert os.path.basename(self.config.mod_ssl_conf_src) == \
                         "options-ssl-nginx-tls13-session-tix-on.conf"


class DetermineDefaultServerRootTest(certbot_test_util.ConfigTestCase):
    """Tests for certbot_nginx._internal.configurator._determine_default_server_root."""

    def _call(self):
        from certbot_nginx._internal.configurator import _determine_default_server_root
        return _determine_default_server_root()

    @mock.patch.dict(os.environ, {"CERTBOT_DOCS": "1"})
    def test_docs_value(self):
        self._test(expect_both_values=True)

    @mock.patch.dict(os.environ, {})
    def test_real_values(self):
        self._test(expect_both_values=False)

    def _test(self, expect_both_values):
        server_root = self._call()

        if expect_both_values:
            assert "/usr/local/etc/nginx" in server_root
            assert "/etc/nginx" in server_root
        else:
            assert server_root in ("/etc/nginx", "/usr/local/etc/nginx")


if __name__ == "__main__":
    sys.exit(pytest.main(sys.argv[1:] + [__file__]))  # pragma: no cover
