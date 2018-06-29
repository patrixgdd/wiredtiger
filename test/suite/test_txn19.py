#!/usr/bin/env python
#
# Public Domain 2014-2018 MongoDB, Inc.
# Public Domain 2008-2014 WiredTiger, Inc.
#
# This is free and unencumbered software released into the public domain.
#
# Anyone is free to copy, modify, publish, use, compile, sell, or
# distribute this software, either in source code form or as a compiled
# binary, for any purpose, commercial or non-commercial, and by any
# means.
#
# In jurisdictions that recognize copyright laws, the author or authors
# of this software dedicate any and all copyright interest in the
# software to the public domain. We make this dedication for the benefit
# of the public at large and to the detriment of our heirs and
# successors. We intend this dedication to be an overt act of
# relinquishment in perpetuity of all present and future rights to this
# software under copyright law.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# test_txn19.py
#   Transactions: test recovery with corrupted log files
#

import fnmatch, os, shutil, time
from wtscenario import make_scenarios
from suite_subprocess import suite_subprocess
import wiredtiger, wttest

# This test uses an artificially small log file limit, and creates
# large records so two fit into a lot file. This allows us to test
# both the case when corruption happens at the beginning of a log file
# (an even number of records have been created), and when corruption
# happens in the middle of a log file (with an odd number of records).

def corrupt(fname, truncate, offset, writeit):
    with open(fname, 'r+') as log:
        if truncate:
            log.truncate()
        if offset:
            if offset < 0:  # Negative offset means seek to the end
                log.seek(0, 2)
            else:
                log.seek(offset)
        if writeit:
            log.write(writeit)

class test_txn19(wttest.WiredTigerTestCase, suite_subprocess):
    base_config = 'log=(archive=false,enabled,file_max=100K),' + \
                  'transaction_sync=(enabled,method=none)'
    conn_config = base_config
    corruption_type = [
        ('removal', dict(kind='removal', f=lambda fname:
            os.remove(fname))),
        ('truncate', dict(kind='truncate', f=lambda fname:
            corrupt(fname, True, 0, None))),
        ('zero-begin', dict(kind='zero', f=lambda fname:
            corrupt(fname, False, 0, '\0' * 4096))),
        ('zero-trunc', dict(kind='zero', f=lambda fname:
            corrupt(fname, True, 0, '\0' * 4096))),
        ('zero-end', dict(kind='zero-end', f=lambda fname:
            corrupt(fname, False, -1, '\0' * 4096))),
        ('garbage-begin', dict(kind='garbage-begin', f=lambda fname:
            corrupt(fname, False, 0, 'Bad!' * 1024))),
        ('garbage-middle', dict(kind='garbage-middle', f=lambda fname:
            corrupt(fname, False, 1024 * 25, 'Bad!' * 1024))),
        ('garbage-end', dict(kind='garbage-end', f=lambda fname:
            corrupt(fname, False, -1, 'Bad!' * 1024))),
    ]
    corruption_pos = [
        ('corrupt=[' + str(x) + ',' + str(y) + '],checkpoint=' + str(z),
           dict(badpos=x,badpos2=y,chkpt=z)) for (x,y,z) in (
               (0, 0, 0), (0, 0, 2), (6, 0, 0), (6, 0, 3), (3, 0, 0),
               (3, 0, 2), (3, 4, 2), (3, 5, 2), (3, 0, 4))]
    nrecords = [('nrecords=10', dict(nrecords=10)),
                ('nrecords=11', dict(nrecords=11))]

    # This function prunes out unnecessary or problematic test cases
    # from the list of scenarios.
    def includeFunc(name, dictarg):
        kind = dictarg['kind']
        badpos = dictarg['badpos']
        chkpt = dictarg['chkpt']

        # badpos == 0 indicates there is no corruption.
        # (i.e. corrupt log file 0, which doesn't exist)
        # We do want to test the case of no corruption, but we don't
        # need to try it against every type of corruption, only one.
        if badpos == 0:
            return kind == 'removal'

        # XXX
        # The removal or truncation of a middle log file (not first or last)
        # that would be used in recovery is not currently handled gracefully.
        if (kind == 'removal' or kind == 'truncate') and \
           badpos != 6 and badpos > chkpt:
            return False

        # All the other cases are valid
        return True

    scenarios = make_scenarios(
        corruption_type, corruption_pos, nrecords,
        include=includeFunc)

    uri = 'table:test_txn19'
    create_params = 'key_format=i,value_format=S'

    # Return the log file number that contains the given record
    # number.  In this test, two records fit into each log file, and
    # after each even record is written, a new log file is created
    # (having no records initially).  The last log file is this
    # (nrecords/2 + 1), given that we start with log 1.
    def record_to_logfile(self, recordnum):
        return recordnum / 2 + 1

    # Returns the first record number in a log file.
    def logfile_to_record(self, logfile):
        return (logfile - 1) * 2

    # Return true if the log file is corrupted.
    # If not corrupted, the log file will produce no errors,
    # and all the records originally written should be recovered.
    def corrupted(self):
        # Badpos == 0 means to do no corruption in any log file
        if self.badpos == 0:
            return False

        # Adding zeroes to the end of a log file is indistinguishable
        # from having a log file that is preallocated that has not been
        # totally filled. One might argue that if this does not occur
        # in the final log file, it could/should have been truncated.
        # At any rate, we consider this particular corruption to be benign.
        if self.kind == 'zero-end':
            return False

        # XXX
        # If garbage is added to an otherwise valid log file, the invalid
        # portion is effectively 'skipped over'.
        if self.kind == 'garbage-end':
            return False
        return True

    def show_logs(self, homedir, msg):
        loglist = []
        for i in range(0, 10):
            basename = 'WiredTigerLog.000000000' + str(i)
            fullname = homedir + os.sep + basename
            if os.path.isfile(fullname):
                loglist.append(i)
                if os.stat(fullname).st_size == 0:
                    self.tty('LOGS ' + msg + ': ' + str(i) + ' is empty')
        self.tty('LOGS ' + msg + ': ' + str(loglist))

    def copy_for_crash_restart(self, olddir, newdir):
        ''' Simulate a crash from olddir and restart in newdir. '''
        # with the connection still open, copy files to new directory
        shutil.rmtree(newdir, ignore_errors=True)
        os.mkdir(newdir)
        for fname in os.listdir(olddir):
            fullname = os.path.join(olddir, fname)
            # Skip lock file on Windows since it is locked
            if os.path.isfile(fullname) and \
                "WiredTiger.lock" not in fullname and \
                "Tmplog" not in fullname and \
                "Preplog" not in fullname:
                shutil.copy(fullname, newdir)

    # Generate a value that is a bit over half the size of the log file.
    def valuegen(self, i):
        return str(i) + 'A' * (1024 * 60)   # ~60K

    # Insert a list of keys
    def inserts(self, keylist):
        c = self.session.open_cursor(self.uri)
        for i in keylist:
            if self.chkpt > 0 and self.logfile_to_record(self.chkpt) == i:
                self.session.checkpoint()
            c[i] = self.valuegen(i)
        c.close()

    def checks(self, expectlist):
        c = self.session.open_cursor(self.uri, None, None)
        gotlist = []
        for key, value in c:
            gotlist.append(key)
            self.assertEqual(self.valuegen(key), value)
        self.assertEqual(expectlist, gotlist)
        c.close()

    def log_number_to_file_name(self, homedir, n):
        self.assertLess(n, 10)  # assuming 1 digit
        return homedir + os.sep + 'WiredTigerLog.000000000' + str(n)

    def corrupt_log(self, homedir):
        if not self.corrupted():
            return
        self.f(self.log_number_to_file_name(homedir, self.badpos))

        # Corrupt a second log file if needed
        if self.badpos2 != 0:
            self.f(self.log_number_to_file_name(homedir, self.badpos2))

    # Return true iff the  log has been damaged in a way that
    # is not detected as a corruption.
    # This includes:
    #  - removal of the last log
    #  - corruption in the middle of the pre-zeroed section of the last log
    def log_corrupt_but_valid(self):
        if self.badpos == self.record_to_logfile(self.nrecords):
            if self.kind == 'removal':
                return True
            # Records in this test are sized about 60K, and 'garbage-middle',
            # places corruption at about the 25K point in the log file.
            # If there are an even number of records, then the last log
            # is mostly empty, the garbage placed in it defines the end
            # of the 'hole'.  If there is an odd number of records, the
            # garbage will be in the middle of the record.
            #
            # XXX
            # Ideally, recovery would view such holes as corruption and
            # require salvaging.
            if self.kind == 'garbage-middle' and self.nrecords % 2 == 0:
                return True
        return False

    # For this test, at least, salvage identifies and fixes all
    # recovery failures.
    def expect_salvage_messages(self):
        return self.expect_recovery_failure()

    def expect_recovery_failure(self):
        return self.corrupted() and \
            self.badpos >= self.chkpt and \
            not self.log_corrupt_but_valid()

    def recovered_records(self):
        if not self.corrupted() or self.chkpt > self.badpos:
            return self.nrecords
        return self.logfile_to_record(self.badpos)

    def test_corrupt_log(self):
        ''' Corrupt the log and restart with different kinds of recovery '''

        # This test creates some data, then simulates a crash with corruption.
        # Then does a restart with recovery, then starts again with salvage,
        # and finally starts again with recovery (adding new records).

        self.session.create(self.uri, self.create_params)
        self.inserts([x for x in range(0, self.nrecords)])
        newdir = "RESTART"
        self.copy_for_crash_restart(self.home, newdir)
        self.close_conn()
        #self.show_logs(newdir, 'before corruption')
        self.corrupt_log(newdir)
        #self.show_logs(newdir, 'after corruption')
        salvage_config = self.base_config + ',log=(recover=salvage)'
        errfile = 'list.err'
        outfile = 'list.out'
        expect_fail = self.expect_recovery_failure()

        # In cases of corruption, we cannot always call wiredtiger_open
        # directly, because there may be a panic, and abort() is called
        # in diagnostic mode which terminates the Python interpreter.
        #
        # Running any wt command externally to Python allows
        # us to observe the failure or success safely.
        # Use -R to force recover=on, which is the default for
        # wiredtiger_open, ()wt utilities normally have recover=error)
        self.runWt(['-h', newdir, '-C', self.base_config, '-R', 'list'],
            errfilename=errfile, outfilename=outfile, failure=expect_fail,
            closeconn=False)

        if expect_fail:
            self.check_file_contains_one_of(errfile,
            ['/log file.*corrupted/', 'WT_ERROR: non-specific WiredTiger error'])
        else:
            self.check_empty_file(errfile)
            self.check_file_contains(outfile, self.uri)

        found_records = self.recovered_records()
        expect = [x for x in range(0, found_records)]

        # If we are salvaging, expect an informational message
        if self.expect_salvage_messages():
            errpat = '.*'
            # Possible messages:
            #   salvage: log files x-y truncated at beginning
            #   salvage: log file x truncated at beginning
            #   salvage: log file x truncated
            #   salvage: log file x removed
            outpat = 'salvage: log file'
        else:
            errpat = '^$'
            outpat = '^$'
        with self.expectedStdoutPattern(outpat):
            with self.expectedStderrPattern(errpat):
                self.conn = self.wiredtiger_open(newdir, salvage_config)
                self.session = self.setUpSessionOpen(self.conn)
                self.checks(expect)

        # Insert a couple more and simulate another crash.
        newdir2 = "RESTART2"
        self.inserts([self.nrecords, self.nrecords + 1])
        expect.extend([self.nrecords, self.nrecords + 1])
        self.copy_for_crash_restart(newdir, newdir2)
        self.checks(expect)
        self.reopen_conn(newdir)
        self.checks(expect)
        self.reopen_conn(newdir2, self.conn_config)
        self.checks(expect)

if __name__ == '__main__':
    wttest.run()
