import unittest
from unittest.mock import Mock
from vizio import TV, TVError, INPUT, INPUTS, redact


class ControlTests(unittest.TestCase):
    def test_input_uses_fresh_current_hash(self):
        tv = TV({'host': '192.168.50.233'})
        tv.request = Mock(side_effect=[{'ITEMS': [{'NAME': 'HDMI-1', 'HASHVAL': 99}]},
                                      {'ITEMS': [{'HASHVAL': 42}]}, {'STATUS': {'RESULT': 'SUCCESS'}}])
        tv.set_input('HDMI-1')
        self.assertEqual(tv.request.call_args.args,
                         (INPUT, {'REQUEST': 'MODIFY', 'VALUE': 'HDMI-1', 'HASHVAL': 42}))
        self.assertEqual(tv.request.call_args.kwargs, {'paired': True})

    def test_invalid_input_does_not_write(self):
        tv = TV({'host': '192.168.50.233'})
        tv.request = Mock(return_value={'ITEMS': [{'NAME': 'HDMI-1'}]})
        with self.assertRaises(TVError): tv.set_input('invalid')
        tv.request.assert_called_once_with(INPUTS, paired=True)

    def test_missing_pairing_fails_before_network(self):
        tv = TV({'host': '192.168.50.233'})
        with self.assertRaisesRegex(TVError, 'Pair first'): tv.key('power-off')

    def test_nested_tokens_redacted(self):
        self.assertEqual(redact({'ITEM': {'AUTH_TOKEN': 'secret'}, 'list': [{'password': 'x'}]}),
                         {'ITEM': {'AUTH_TOKEN': '[redacted]'}, 'list': [{'password': '[redacted]'}]})


if __name__ == '__main__': unittest.main()
