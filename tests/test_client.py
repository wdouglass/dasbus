#
# Copyright (C) 2019  Red Hat, Inc.  All rights reserved.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301
# USA
#
import unittest
from textwrap import dedent
from unittest.mock import Mock

from dasbus.client.handler import ClientObjectHandler, GLibClient
from dasbus.client.proxy import ObjectProxy, disconnect_proxy, InterfaceProxy
from dasbus.constants import DBUS_FLAG_NONE
from dasbus.error import ErrorMapper, DBusError, ErrorRule
from dasbus.signal import Signal
from dasbus.specification import DBusSpecification
from dasbus.typing import get_variant, get_variant_type, VariantType

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio


class FakeException(Exception):
    """Fake exception from DBus calls."""
    pass


class VariantTypeFactory(object):
    """Return same objects for same type strings.

    This factory allows to easily test calls with variant types.
    """

    def __init__(self):
        self._types = {}
        self._new = VariantType.new
        self._str = VariantType.__str__
        self._repr = VariantType.__repr__

    def set_up(self):
        VariantType.new = self.get
        VariantType.__str__ = VariantType.dup_string
        VariantType.__repr__ = VariantType.dup_string

    def tear_down(self):
        VariantType.new = self._new
        VariantType.__str__ = self._str
        VariantType.__repr__ = self._repr

    def get(self, type_string):
        return self._types.setdefault(type_string, self._new(type_string))


class DBusClientTestCase(unittest.TestCase):
    """Test DBus clinet support."""

    NO_REPLY = get_variant("()", ())

    def setUp(self):
        self.maxDiff = None
        self.message_bus = Mock()
        self.connection = self.message_bus.connection
        self.error_mapper = ErrorMapper()
        self.service_name = "my.service"
        self.object_path = "/my/object"
        self.handler = None
        self.proxy = None

        self.variant_type_factory = VariantTypeFactory()
        self.variant_type_factory.set_up()

    def tearDown(self):
        self.variant_type_factory.tear_down()

    def test_variant_type_factory(self):
        """Test the variant type factory."""
        self.assertEqual(str(get_variant_type("s")), "s")
        self.assertEqual(repr(get_variant_type("i")), "i")

        self.assertEqual(get_variant_type("s"), get_variant_type("s"))
        self.assertEqual(get_variant_type("i"), get_variant_type("i"))

        self.assertNotEqual(get_variant_type("b"), get_variant_type("i"))
        self.assertNotEqual(get_variant_type("s"), get_variant_type("u"))

    def _create_proxy(self, xml):
        """Create a proxy with a mocked message bus."""
        self.proxy = ObjectProxy(
            self.message_bus,
            self.service_name,
            self.object_path,
            error_mapper=self.error_mapper
        )
        self.handler = self.proxy._handler
        self.handler._specification = DBusSpecification.from_xml(xml)

    def test_introspect(self):
        """Test the introspection."""
        self._set_reply(get_variant("(s)", (dedent("""
        <node>
            <interface name="Interface">
                <method name="Method1"/>
            </interface>
        </node>
        """), )))

        self.handler = ClientObjectHandler(
            self.message_bus,
            self.service_name,
            self.object_path
        )
        self.assertIsNotNone(self.handler.specification)
        self._check_call(
            "org.freedesktop.DBus.Introspectable",
            "Introspect",
            reply_type=get_variant_type("(s)")
        )

        self.assertIn(
            DBusSpecification.Method("Method1", "Interface", None, None),
            self.handler.specification.members
        )

    def test_method(self):
        """Test the method proxy."""
        self._create_proxy("""
        <node>
            <interface name="Interface">
                <method name="Method1"/>
                <method name="Method2">
                    <arg direction="in" name="x" type="i"/>
                </method>
                <method name="Method3">
                    <arg direction="out" name="return" type="i"/>
                </method>
                <method name="Method4">
                    <arg direction="in" name="x" type="ad"/>
                    <arg direction="in" name="y" type="o"/>
                    <arg direction="out" name="return" type="(ib)"/>
                </method>
                <method name="Method5">
                    <arg direction="out" name="return_x" type="i"/>
                    <arg direction="out" name="return_y" type="i"/>
                </method>
            </interface>
        </node>
        """)

        self.assertTrue(callable(self.proxy.Method1))
        self.assertEqual(self.proxy.Method1, self.proxy.Method1)

        self._set_reply(self.NO_REPLY)
        self.assertEqual(self.proxy.Method1(), None)
        self._check_call(
            "Interface",
            "Method1"
        )

        self._set_reply(self.NO_REPLY)
        self.assertEqual(self.proxy.Method2(1), None)
        self._check_call(
            "Interface",
            "Method2",
            parameters=get_variant("(i)", (1, ))
        )

        self._set_reply(get_variant("(i)", (0, )))
        self.assertEqual(self.proxy.Method3(), 0)
        self._check_call(
            "Interface",
            "Method3",
            reply_type=get_variant_type("(i)")
        )

        self._set_reply(get_variant("((ib))", ((1, True), )))
        self.assertEqual(self.proxy.Method4([1.2, 2.3], "/my/path"),
                         (1, True))
        self._check_call(
            "Interface",
            "Method4",
            parameters=get_variant("(ado)", ([1.2, 2.3], "/my/path")),
            reply_type=get_variant_type("((ib))")
        )

        self._set_reply(get_variant("(ii)", (1, 2)))
        self.assertEqual(self.proxy.Method5(), (1, 2))
        self._check_call(
            "Interface",
            "Method5",
            reply_type=get_variant_type("(ii)")
        )

        # Handle unregistered remote exception.
        self._set_reply(Gio.DBusError.new_for_dbus_error(
            "org.test.Unknown",
            "My message."
        ))

        with self.assertRaises(DBusError) as cm:
            self.proxy.Method1()

        self.assertTrue("My message." in str(cm.exception))

        # Handle registered remote exception.
        self.error_mapper.add_rule(ErrorRule(
            exception_type=FakeException,
            error_name="org.test.Unknown"
        ))

        self._set_reply(Gio.DBusError.new_for_dbus_error(
            "org.test.Unknown",
            "My message."
        ))

        with self.assertRaises(FakeException) as cm:
            self.proxy.Method1()

        self.assertEqual(str(cm.exception), "My message.")

        # Handle local exception.
        self._set_reply(Exception("My message."))

        with self.assertRaises(Exception) as cm:
            self.proxy.Method1()

        self.assertEqual(str(cm.exception), "My message.")

        # Test invalid method.
        with self.assertRaises(AttributeError) as cm:
            self.proxy.MethodInvalid()

        self.assertEqual(
            "DBus object has no attribute 'MethodInvalid'.",
            str(cm.exception)
        )

        # Test invalid attribute.
        with self.assertRaises(AttributeError) as cm:
            self.proxy.Method1 = lambda: 1

        self.assertEqual(
            "Can't set DBus attribute 'Method1'.",
            str(cm.exception)
        )

    def test_invalid_method_result(self):
        """Test a method proxy with an invalid result."""
        self._create_proxy("""
        <node>
            <interface name="Interface">
                <method name="Method">
                    <arg direction="out" name="return" type="t"/>
                </method>
            </interface>
        </node>
        """)

        self._set_reply(get_variant("i", -1))
        with self.assertRaises(TypeError):
            self.proxy.Method()

    def _set_reply(self, reply_value):
        """Set the reply of the DBus call."""
        self.connection.call_sync.reset_mock()

        if isinstance(reply_value, Exception):
            self.connection.call_sync.side_effect = reply_value
        else:
            self.connection.call_sync.return_value = reply_value

    def _check_call(self, interface_name, method_name, parameters=None,
                    reply_type=None):
        """Check the DBus call."""
        self.connection.call_sync.assert_called_once_with(
            self.service_name,
            self.object_path,
            interface_name,
            method_name,
            parameters,
            reply_type,
            DBUS_FLAG_NONE,
            GLibClient.DBUS_TIMEOUT_NONE,
            None
        )

        self.connection.call_sync.reset_mock()

    def test_async_method(self):
        """Test asynchronous calls of a method proxy."""
        self._create_proxy("""
        <node>
            <interface name="Interface">
                <method name="Method1"/>
                <method name="Method2">
                    <arg direction="in" name="x" type="i"/>
                    <arg direction="in" name="y" type="i"/>
                    <arg direction="out" name="return" type="i"/>
                </method>
            </interface>
        </node>
        """)
        callback = Mock()
        callback_args = ("A", "B")
        self.proxy.Method1(callback=callback, callback_args=callback_args)
        self._check_async_call(
            "Interface",
            "Method1",
            callback,
            callback_args
        )

        self._finish_async_call(self.NO_REPLY, callback, callback_args)
        callback.assert_called_once_with(None, "A", "B")

        callback = Mock()
        callback_args = ("A", "B")
        self.proxy.Method2(
            1, 2, callback=callback, callback_args=callback_args
        )
        self._check_async_call(
            "Interface",
            "Method2",
            callback,
            callback_args,
            get_variant("(ii)", (1, 2)),
            get_variant_type("(i)")
        )

        self._finish_async_call(
            get_variant("(i)", (3, )),
            callback,
            callback_args
        )
        callback.assert_called_once_with(3, "A", "B")

        self.error_mapper.add_rule(ErrorRule(
            exception_type=FakeException,
            error_name="org.test.Unknown"
        ))

        callback = Mock()
        callback_args = ("A", "B")

        error = Gio.DBusError.new_for_dbus_error(
            "org.test.Unknown",
            "My message."
        )

        with self.assertRaises(FakeException) as cm:
            self._finish_async_call(error, callback, callback_args)

        self.assertEqual(str(cm.exception), "My message.")
        callback.assert_not_called()

    def _check_async_call(self, interface_name, method_name, callback,
                          callback_args, parameters=None, reply_type=None):
        """Check the asynchronous DBus call."""
        self.connection.call.assert_called_once_with(
            self.service_name,
            self.object_path,
            interface_name,
            method_name,
            parameters,
            reply_type,
            DBUS_FLAG_NONE,
            GLibClient.DBUS_TIMEOUT_NONE,
            callback=GLibClient._async_call_finish,
            user_data=(
                self.handler._method_callback,
                (callback, callback_args)
            )
        )

        self.connection.call.reset_mock()

    def _finish_async_call(self, result, callback, callback_args):
        """Finish the asynchronous call."""
        def _call_finish(result_object):
            if isinstance(result_object, Exception):
                raise result_object

            return (result_object,)

        def _callback(finish, *args):
            callback(finish(), *args)

        GLibClient._async_call_finish(
            source_object=Mock(call_with_unix_fd_list_finish=_call_finish),
            result_object=result,
            user_data=(
                self.handler._method_callback,
                (_callback, callback_args)
            )
        )

    def test_property(self):
        """Test the property proxy."""
        self._create_proxy("""
        <node>
            <interface name="Interface">
                <property name="Property1" type="i" access="readwrite" />
                <property name="Property2" type="s" access="read" />
                <property name="Property3" type="b" access="write" />
            </interface>
        </node>
        """)

        self._set_reply(self.NO_REPLY)
        self.proxy.Property1 = 10
        self._check_set_property("Property1", get_variant("i", 10))

        self._set_reply(get_variant("(v)", (get_variant("i", 20), )))
        self.assertEqual(self.proxy.Property1, 20)
        self._check_get_property("Property1")

        with self.assertRaises(AttributeError) as cm:
            self.proxy.Property2 = "World"

        self.assertEqual(str(cm.exception), "Can't set DBus property.")

        self._set_reply(get_variant("(v)", (get_variant("s", "Hello"), )))
        self.assertEqual(self.proxy.Property2, "Hello")
        self._check_get_property("Property2")

        self._set_reply(self.NO_REPLY)
        self.proxy.Property3 = False
        self._check_set_property("Property3", get_variant("b", False))

        with self.assertRaises(AttributeError) as cm:
            self.fail(self.proxy.Property3)

        self.assertEqual(str(cm.exception), "Can't read DBus property.")

        with self.assertRaises(AttributeError) as cm:
            self.proxy.PropertyInvalid = 0

        self.assertEqual(
            "DBus object has no attribute 'PropertyInvalid'.",
            str(cm.exception)
        )

        with self.assertRaises(AttributeError) as cm:
            self.fail(self.proxy.PropertyInvalid)

        self.assertEqual(
            "DBus object has no attribute 'PropertyInvalid'.",
            str(cm.exception)
        )

    def _check_set_property(self, name, value):
        """Check the DBus call that sets a property."""
        self._check_call(
            "org.freedesktop.DBus.Properties",
            "Set",
            get_variant("(ssv)", ("Interface", name, value)),
            None
        )

    def _check_get_property(self, name):
        """Check the DBus call that gets a property."""
        self._check_call(
            "org.freedesktop.DBus.Properties",
            "Get",
            get_variant("(ss)", ("Interface", name)),
            get_variant_type("(v)")
        )

    def test_signal(self):
        """Test the signal publishing."""
        self._create_proxy("""
        <node>
            <interface name="Interface">
                <signal name="Signal1" />
                <signal name="Signal2">
                    <arg direction="out" name="x" type="i"/>
                    <arg direction="out" name="y" type="s"/>
                </signal>
            </interface>
        </node>
        """)

        self.assertIsInstance(self.proxy.Signal1, Signal)
        self.assertEqual(self.proxy.Signal1, self.proxy.Signal1)

        self._check_signal("Interface", "Signal1", self.proxy.Signal1.emit)
        self._emit_signal(self.NO_REPLY, self.proxy.Signal1.emit)
        self.assertEqual(len(self.handler._subscriptions), 2)

        self._check_signal("Interface", "Signal2", self.proxy.Signal2.emit)
        self._emit_signal(get_variant("(is)", (1, "Test")),
                          self.proxy.Signal2.emit)
        self.assertEqual(len(self.handler._subscriptions), 4)

        with self.assertRaises(AttributeError) as cm:
            self.fail(self.proxy.SignalInvalid)

        self.assertEqual(
            "DBus object has no attribute 'SignalInvalid'.",
            str(cm.exception)
        )

        with self.assertRaises(AttributeError) as cm:
            self.proxy.Signal1 = self.handler._signal_factory()

        self.assertEqual(
            "Can't set DBus attribute 'Signal1'.",
            str(cm.exception)
        )

        self.proxy.Signal1.connect(Mock())
        self.proxy.Signal2.connect(Mock())

        disconnect_proxy(self.proxy)
        self.assertEqual(self.connection.signal_unsubscribe.call_count, 2)
        self.assertEqual(self.handler._subscriptions, [])
        self.assertEqual(self.proxy.Signal1._callbacks, [])
        self.assertEqual(self.proxy.Signal2._callbacks, [])

    def _check_signal(self, interface_name, signal_name, signal_callback):
        """Check the DBus signal subscription."""
        self.connection.signal_subscribe.assert_called_once_with(
            self.service_name,
            interface_name,
            signal_name,
            self.object_path,
            None,
            DBUS_FLAG_NONE,
            callback=GLibClient._signal_callback,
            user_data=(self.handler._signal_callback, (signal_callback, ))
        )
        self.connection.signal_subscribe.reset_mock()

    def _emit_signal(self, parameters, signal_callback):
        """Emit a DBus signal."""
        GLibClient._signal_callback(
            self.connection,
            None,
            self.object_path,
            None,
            None,
            parameters=parameters,
            user_data=(self.handler._signal_callback, (signal_callback,))
        )

    def test_error(self):
        """Test the error handling."""
        error = Exception("My message.")
        self.assertEqual(
            GLibClient.is_remote_error(error),
            False
        )

        error = Gio.DBusError.new_for_dbus_error(
            "org.test.Error",
            "My message."
        )
        self.assertEqual(
            GLibClient.is_remote_error(error),
            True
        )
        self.assertEqual(
            GLibClient.get_remote_error_name(error),
            "org.test.Error",
        )
        self.assertEqual(
            GLibClient.get_remote_error_message(error),
            "My message."
        )

    def _create_interface_proxy(self, xml, interface_name):
        """Create an interface proxy with a mocked message bus."""
        self.proxy = InterfaceProxy(
            self.message_bus,
            self.service_name,
            self.object_path,
            interface_name=interface_name,
            error_mapper=self.error_mapper
        )
        self.handler = self.proxy._handler
        self.handler._specification = DBusSpecification.from_xml(xml)

    def test_interface_proxy(self):
        """Test the interface proxy."""
        xml = """
        <node>
            <interface name="Interface1">
                <method name="Method1"/>
            </interface>
            <interface name="Interface2">
                <method name="Method2"/>
            </interface>
            <interface name="Interface3">
                <method name="Method1"/>
                <method name="Method2"/>
                <method name="Method3"/>
            </interface>
        </node>
        """

        # Test the first interface.
        self._create_interface_proxy(xml, "Interface1")

        # Test a valid method.
        self._set_reply(self.NO_REPLY)
        self.assertEqual(self.proxy.Method1(), None)
        self._check_call("Interface1", "Method1")

        # Test invalid methods.
        with self.assertRaises(AttributeError):
            self.proxy.Method2()

        with self.assertRaises(AttributeError):
            self.proxy.Method3()

        # Test the second interface.
        self._create_interface_proxy(xml, "Interface2")

        # Test a valid method.
        self._set_reply(self.NO_REPLY)
        self.assertEqual(self.proxy.Method2(), None)
        self._check_call("Interface2", "Method2")

        # Test invalid methods.
        with self.assertRaises(AttributeError):
            self.proxy.Method1()

        with self.assertRaises(AttributeError):
            self.proxy.Method3()

        # Test the third interface.
        self._create_interface_proxy(xml, "Interface3")

        # Test a valid method.
        self._set_reply(self.NO_REPLY)
        self.assertEqual(self.proxy.Method1(), None)
        self._check_call("Interface3", "Method1")

        self._set_reply(self.NO_REPLY)
        self.assertEqual(self.proxy.Method2(), None)
        self._check_call("Interface3", "Method2")

        self._set_reply(self.NO_REPLY)
        self.assertEqual(self.proxy.Method3(), None)
        self._check_call("Interface3", "Method3")

        # Test an invalid method.
        with self.assertRaises(AttributeError):
            self.proxy.Method4()
