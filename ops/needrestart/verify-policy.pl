#!/usr/bin/perl
# Read-only tests: no needrestart scan, service restart or clock changes.
use strict;
use warnings;
use Test::More;
our $test_now;
BEGIN { *CORE::GLOBAL::time = sub { $main::test_now }; }
our %nrconf;
my $path = shift @ARGV or die "usage: perl verify-policy.pl policy.conf\n";
open my $fh, '<', $path or die "open $path: $!";
my $policy = do { local $/; <$fh> };
close $fh;

for my $case (
    [1791674498, 0, 'before deadline'],
    [1791674499, 1, 'at deadline'],
    [1791674500, 1, 'after deadline'],
) {
    ($test_now, my $expected, my $label) = @$case;
    %nrconf = (override_rc => {qr(^unrelated\.service$) => 0});
    eval $policy;
    die $@ if $@;
    my $decision = sub {
        my ($service) = @_;
        for my $re (keys %{$nrconf{override_rc}}) {
            return $nrconf{override_rc}{$re} if $service =~ /$re/;
        }
        return 1;
    };
    is($decision->('bb-squeeze-paper.service'), $expected, $label);
    is($decision->('ssh.service'), 1, "$label: other service unchanged");
    is($decision->('other-bb-squeeze-paper.service'), 1, "$label: exact name only");
    is($decision->('bb-squeeze-paper.service.extra'), 1, "$label: no suffix match");
    is($decision->('unrelated.service'), 0, "$label: existing override preserved");
}
done_testing();
