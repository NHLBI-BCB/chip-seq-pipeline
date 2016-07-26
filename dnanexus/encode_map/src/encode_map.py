#!/usr/bin/env python
# ENCODE_map 0.0.1

import os
import subprocess
import shlex
import time
import re
from multiprocessing import Pool, cpu_count
import dxpy
import common
import logging

logger = logging.getLogger(__name__)
logger.addHandler(dxpy.DXLogHandler())
logger.propagate = False


def flagstat_parse(fname):
    with open(fname, 'r') as flagstat_file:
        if not flagstat_file:
            return None
        flagstat_lines = flagstat_file.read().splitlines()

    qc_dict = {
        # values are regular expressions,
        # will be replaced with scores [hiq, lowq]
        'in_total': 'in total',
        'duplicates': 'duplicates',
        'mapped': 'mapped',
        'paired_in_sequencing': 'paired in sequencing',
        'read1': 'read1',
        'read2': 'read2',
        'properly_paired': 'properly paired',
        'with_self_mate_mapped': 'with itself and mate mapped',
        'singletons': 'singletons',
        # i.e. at the end of the line
        'mate_mapped_different_chr': 'with mate mapped to a different chr$',
        # RE so must escape
        'mate_mapped_different_chr_hiQ':
            'with mate mapped to a different chr \(mapQ>=5\)'
    }

    for (qc_key, qc_pattern) in qc_dict.items():
        qc_metrics = next(re.split(qc_pattern, line)
                          for line in flagstat_lines
                          if re.search(qc_pattern, line))
        (hiq, lowq) = qc_metrics[0].split(' + ')
        qc_dict[qc_key] = [int(hiq.rstrip()), int(lowq.rstrip())]

    return qc_dict


def resolve_reference():
    # assume the reference file is the only .fa or .fna file
    return next((f for f in os.listdir(".") if f.endswith('.fa') or f.endswith('.fna') or f.endswith('.fa.gz') or f.endswith('.fna.gz')), None)


@dxpy.entry_point("postprocess")
def postprocess(indexed_reads, unmapped_reads, reference_tar, bwa_version, samtools_version):

    print "In postprocess with:"

    if samtools_version == "0.1.19":
        samtools = "/usr/local/bin/samtools-0.1.19/samtools"
    elif samtools_version == "1.0":
        samtools = "/usr/local/bin/samtools-1.0/bin/samtools"
    else:
        samtools = "/usr/local/bin/samtools-0.1.19/samtools"

    if bwa_version == "0.7.7":
        bwa = "bwa0.7.7"
    elif bwa_version == "0.7.10":
        bwa = "bwa0.7.10"
    else:
        print "BWA version %s not supported, defaulting to 0.7.7"
        bwa = "bwa0.7.7"

    print "samtools version: %s" %(samtools)
    print "bwa version %s" %(bwa)

    indexed_reads_filenames = []
    unmapped_reads_filenames = []
    for i,reads in enumerate(indexed_reads):
        read_pair_number = i+1

        fn = dxpy.describe(reads)['name']
        print "indexed_reads %d: %s" %(read_pair_number, fn)
        indexed_reads_filenames.append(fn)
        dxpy.download_dxfile(reads,fn)

        unmapped = unmapped_reads[i]
        fn = dxpy.describe(unmapped)['name']
        print "unmapped reads %d: %s" %(read_pair_number, fn)
        unmapped_reads_filenames.append(fn)
        dxpy.download_dxfile(unmapped,fn)

    reference_tar_filename = dxpy.describe(reference_tar)['name']
    print "reference_tar: %s" %(reference_tar_filename)
    dxpy.download_dxfile(reference_tar, reference_tar_filename)
    # extract the reference files from the tar
    if reference_tar_filename.endswith('.gz') or reference_tar_filename.endswith('.tgz'):
        tar_command = 'tar -xzv --no-same-owner --no-same-permissions -f %s' %(reference_tar_filename)
    else:
        tar_command = 'tar -xv --no-same-owner --no-same-permissions -f %s' %(reference_tar_filename)
    print "Unpacking %s" %(reference_tar_filename)
    print tar_command
    print subprocess.check_output(shlex.split(tar_command))
    reference_filename = resolve_reference()

    paired_end = len(indexed_reads) == 2

    if paired_end:
        r1_basename = unmapped_reads_filenames[0].rstrip('.gz').rstrip('.fq').rstrip('.fastq')
        r2_basename = unmapped_reads_filenames[1].rstrip('.gz').rstrip('.fq').rstrip('.fastq')
        reads_basename = r1_basename + r2_basename
    else:
        reads_basename = unmapped_reads_filenames[0].rstrip('.gz').rstrip('.fq').rstrip('.fastq')
    raw_bam_filename = '%s.raw.srt.bam' %(reads_basename)
    raw_bam_mapstats_filename = '%s.raw.srt.bam.flagstat.qc' %(reads_basename)

    if paired_end:
        reads1_filename = indexed_reads_filenames[0]
        reads2_filename = indexed_reads_filenames[1]
        unmapped_reads1_filename = unmapped_reads_filenames[0]
        unmapped_reads2_filename = unmapped_reads_filenames[1]
        raw_sam_filename = reads_basename + ".raw.sam"
        badcigar_filename = "badreads.tmp"
        steps = [ "%s sampe -P %s %s %s %s %s" %(bwa, reference_filename, reads1_filename, reads2_filename, unmapped_reads1_filename, unmapped_reads2_filename),
                  "tee %s" %(raw_sam_filename),
                  r"""awk 'BEGIN {FS="\t" ; OFS="\t"} ! /^@/ && $6!="*" { cigar=$6; gsub("[0-9]+D","",cigar); n = split(cigar,vals,"[A-Z]"); s = 0; for (i=1;i<=n;i++) s=s+vals[i]; seqlen=length($10) ; if (s!=seqlen) print $1"\t" ; }'""",
                  "sort",
                  "uniq" ]
        out,err = common.run_pipe(steps,badcigar_filename)
        if err:
            print "sampe error: %s" %(err)

        steps = [ "cat %s" %(raw_sam_filename),
                  "grep -v -F -f %s" %(badcigar_filename)]
    else: #single end
        reads_filename = indexed_reads_filenames[0]
        unmapped_reads_filename = unmapped_reads_filenames[0]
        steps = [ "%s samse %s %s %s" %(bwa, reference_filename, reads_filename, unmapped_reads_filename) ]
    if samtools_version == "0.1.9":
        steps.extend(["%s view -Su -" %(samtools),
                      "%s sort - %s" %(samtools, raw_bam_filename.rstrip('.bam')) ]) # samtools adds .bam
    else:
        steps.extend(["%s view -@%d -Su -" %(samtools, cpu_count()),
                      "%s sort -@%d - %s" %(samtools, cpu_count(), raw_bam_filename.rstrip('.bam')) ]) # samtools adds .bam
    print "Running pipe:"
    print steps
    out,err = common.run_pipe(steps)

    if out:
        print "samtools output: %s" %(out)
    if err:
        print "samtools error: %s" %(err)

    with open(raw_bam_mapstats_filename, 'w') as fh:
        subprocess.check_call(shlex.split("%s flagstat %s" \
            %(samtools, raw_bam_filename)), stdout=fh)

    print subprocess.check_output('ls', shell=True)
    mapped_reads = dxpy.upload_local_file(raw_bam_filename)
    mapping_statistics = dxpy.upload_local_file(raw_bam_mapstats_filename)
    flagstat_qc = flagstat_parse(raw_bam_mapstats_filename)

    output = {'mapped_reads': dxpy.dxlink(mapped_reads),
              'mapping_statistics': dxpy.dxlink(mapping_statistics),
              'n_mapped_reads': flagstat_qc.get('mapped')[0]  # 0 is index for hi-q reads
              }
    print "Returning from post with output: %s" %(output)
    return output


@dxpy.entry_point("process")
def process(reads_file, reference_tar, bwa_aln_params, bwa_version):
    # reads_file, reference_tar should be links to file objects.
    # reference_tar should be a tar of files generated by bwa index and
    # the tar should be uncompressed to avoid repeating the decompression.

    print "In process"

    if bwa_version == "0.7.7":
        bwa = "bwa0.7.7"
    elif bwa_version == "0.7.10":
        bwa = "bwa0.7.10"
    else:
        bwa = "bwa0.7.7"
    print "Using bwa version %s" %(bwa_version)

    # Generate filename strings and download the files to the local filesystem
    reads_filename = dxpy.describe(reads_file)['name']
    reads_basename = reads_filename
    # the order of this list is important.  It strips from the right inward, so
    # the expected right-most extensions should appear first (like .gz)
    for extension in ['.gz', '.fq', '.fastq', '.fa', '.fasta']:
        reads_basename = reads_basename.rstrip(extension)
    reads_file = dxpy.download_dxfile(reads_file,reads_filename)

    reference_tar_filename = dxpy.describe(reference_tar)['name']
    reference_tar_file = dxpy.download_dxfile(reference_tar,reference_tar_filename)
    # extract the reference files from the tar
    if reference_tar_filename.endswith('.gz') or reference_tar_filename.endswith('.tgz'):
        tar_command = 'tar -xzv --no-same-owner --no-same-permissions -f %s' %(reference_tar_filename)
    else:
        tar_command = 'tar -xv --no-same-owner --no-same-permissions -f %s' %(reference_tar_filename)
    print "Unpacking %s" %(reference_tar_filename)
    print tar_command
    print subprocess.check_output(shlex.split(tar_command))
    reference_filename = resolve_reference()
    print "Using reference file: %s" %(reference_filename)

    print subprocess.check_output('ls -l', shell=True)

    #generate the suffix array index file
    sai_filename = '%s.sai' %(reads_basename)
    with open(sai_filename,'w') as sai_file:
        # Build the bwa command and call bwa
        bwa_command = "%s aln %s -t %d %s %s" \
            %(bwa, bwa_aln_params, cpu_count(), reference_filename, reads_filename)
        print bwa_command
        subprocess.check_call(shlex.split(bwa_command), stdout=sai_file) 

    print subprocess.check_output('ls -l', shell=True)

    # Upload the output to the DNAnexus project
    print "Uploading %s" %(sai_filename)
    sai_dxfile = dxpy.upload_local_file(sai_filename)
    process_output = { "output": dxpy.dxlink(sai_dxfile) }
    print "Returning from process:"
    print process_output
    return process_output


@dxpy.entry_point("main")
def main(reads1, reads2, crop_length, reference_tar,
         bwa_version, bwa_aln_params, samtools_version, input_JSON, debug):

    # Main entry-point.  Parameter defaults assumed to come from dxapp.json.
    # reads1, reference_tar, reads2 are links to DNAnexus files or None

    if debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    # if there is input_JSON, it over-rides any explicit parameters

    if input_JSON:
        if 'reads1' in input_JSON:
            reads1 = input_JSON['reads1']
        if 'reads2' in input_JSON:
            reads2 = input_JSON['reads2']
        if 'reference_tar' in input_JSON:
            reference_tar = input_JSON['reference_tar']
        if 'bwa_aln_params' in input_JSON:
            bwa_aln_params = input_JSON['bwa_aln_params']
        if 'bwa_version' in input_JSON:
            bwa_version = input_JSON['bwa_version']
        if 'samtools_version' in input_JSON:
            samtools_version = input_JSON['samtools_version']

    if not reads1:
        logger.error('reads1 is required, explicitly or in input_JSON')
        raise Exception

    # This spawns only one or two subjobs for single- or paired-end,
    # respectively.  It could also download the files, chunk the reads,
    # and spawn multiple subjobs.

    # Files are downloaded later by subjobs into their own filesystems
    # and uploaded to the project.

    # Initialize file handlers for input files.

    paired_end = reads2 is not None
    unmapped_reads = [r for r in [reads1, reads2] if r]

    subjobs = []



    for reads in unmapped_reads:
        mapping_subjob_input = {
            "reads_file": reads,
            "reference_tar": reference_tar,
            "bwa_aln_params": bwa_aln_params,
            "bwa_version": bwa_version}
        logger.info("Submitting: %s" % (subjob_input))
        subjobs.append(dxpy.new_dxjob(subjob_input, "process"))

    # Create the job that will perform the "postprocess" step.
    # depends_on=subjobs, so blocks on all subjobs

    postprocess_job = dxpy.new_dxjob(
        fn_input={
            "indexed_reads": [subjob.get_output_ref("output") for subjob in subjobs],
            "unmapped_reads": unmapped_reads,
            "reference_tar": reference_tar,
            "bwa_version": bwa_version,
            "samtools_version": samtools_version},
        fn_name="postprocess",
        depends_on=subjobs)

    mapped_reads = postprocess_job.get_output_ref("mapped_reads")
    mapping_statistics = postprocess_job.get_output_ref("mapping_statistics")
    n_mapped_reads = postprocess_job.get_output_ref("n_mapped_reads")

    output = {
        "mapped_reads": mapped_reads,
        "mapping_statistics": mapping_statistics,
        "paired_end": paired_end,
        "n_mapped_reads": n_mapped_reads
    }
    output.update({'output_JSON': output.copy()})

    print "Exiting with output: %s" %(output)
    return output

dxpy.run()