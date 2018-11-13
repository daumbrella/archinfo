import struct as _struct
import platform as _platform
import re
from archinfo.archerror import ArchError

try:
    import pyvex as _pyvex
except ImportError:
    _pyvex = None

try:
    import unicorn as _unicorn
except ImportError:
    _unicorn = None

try:
    import capstone as _capstone
except ImportError:
    _capstone = None

import logging
l = logging.getLogger('archinfo.arch')
l.addHandler(logging.NullHandler())

class Endness:
    """ Endness specifies the byte order for integer values

    :cvar LE:      little endian, least significant byte is stored at lowest address
    :cvar BE:      big endian, most significant byte is stored at lowest address 
    :cvar ME:      Middle-endian. Yep.
    """
    LE = "Iend_LE"
    BE = "Iend_BE"
    ME = 'Iend_ME'

class Arch(object):
    """
    A collection of information about a given architecture. This class should be subclasses for each different
    architecture, and then that subclass should be registered with the ``register_arch`` method.

    A good number of assumptions are made that code is being processed under the VEX IR - for instance, it is expected
    the register file offsets are expected to match code generated by PyVEX.

    Arches maybe compared with == and !=.

    :ivar str name: The name of the arch
    :ivar int bits: The number of bits in a word
    :ivar str vex_arch: The VEX enum name used to identify this arch
    :ivar str qemu_name: The name used by QEMU to identify this arch
    :ivar str ida_processor: The processor string used by IDA to identify this arch
    :ivar str triplet: The triplet used to identify a linux system on this arch
    :ivar int max_inst_bytes: The maximum number of bytes in a single instruction
    :ivar int ip_offset: The offset of the instruction pointer in the register file
    :ivar int sp_offset: The offset of the stack pointer in the register file
    :ivar int bp_offset: The offset of the base pointer in the register file
    :ivar int lr_offset: The offset of the link register (return address) in the register file
    :ivar int ret_offset: The offset of the return value register in the register file
    :ivar bool vex_conditional_helpers: Whether libVEX will generate code to process the conditional flags for this
            arch using ccalls
    :ivar int syscall_num_offset: The offset in the register file where the syscall number is stored
    :ivar bool call_pushes_ret: Whether this arch's call instruction causes a stack push
    :ivar int stack_change: The change to the stack pointer caused by a push instruction
    :ivar str memory_endness: The endness of memory, as a VEX enum
    :ivar str register_endness: The endness of registers, as a VEX enum. Should usually be same as above
    :ivar dict sizeof: A mapping from C type to variable size in bits
    :ivar cs_arch: The capstone arch value for this arch
    :ivar cs_mode: The capstone mode value for this arch
    :ivar uc_arch: The unicorn engine arch value for this arch
    :ivar uc_mode: The unicorn engine mode value for this arch
    :ivar uc_const: The unicorn engine constants module for this arch
    :ivar uc_prefix: The prefix used for variables in the unicorn engine constants module
    :ivar list function_prologs: A list of regular expressions matching the bytes for common function prologues
    :ivar list function_epilogs: A list of regular expressions matching the bytes for common function epilogues
    :ivar str ret_instruction: The bytes for a return instruction
    :ivar str nop_instruction: The bytes for a nop instruction
    :ivar int instruction_alignment: The instruction alignment requirement
    :ivar list default_register_values: A weird listing describing how registers should be initialized for purposes of
            sanity
    :ivar dict entry_register_values: A mapping from register name to a description of the value that should be in it
            at program entry on linux
    :ivar list default_symbolic_register: Honestly, who knows what this is supposed to do. Fill it with the names of
            the general purpose registers.
    :ivar dict register_names: A mapping from register file offset to register name
    :ivar dict registers: A mapping from register name to a tuple of (register file offset, size in bytes)
    :ivar list lib_paths: A listing of common locations where shared libraries for this architecture may be found
    :ivar str got_section_name: The name of the GOT section in ELFs
    :ivar str ld_linux_name: The name of the linux dynamic loader program
    :cvar int byte_width: the number of bits in a byte.
    :ivar TLSArchInfo elf_tls: A description of how thread-local storage works
    """
    byte_width = 8

    def __init__(self, endness):
        if endness not in (Endness.LE, Endness.BE, Endness.ME):
            raise ArchError('Must pass a valid VEX endness: Endness.LE or Endness.BE')

        if _pyvex:
            self.vex_archinfo = _pyvex.default_vex_archinfo()
        if endness == Endness.BE:
            if self.vex_archinfo:
                self.vex_archinfo['endness'] = _pyvex.vex_endness_from_string('VexEndnessBE')
            self.memory_endness = Endness.BE
            self.register_endness = Endness.BE
            if _capstone and self.cs_mode is not None:
                self.cs_mode -= _capstone.CS_MODE_LITTLE_ENDIAN
                self.cs_mode += _capstone.CS_MODE_BIG_ENDIAN
            self.ret_instruction = reverse_ends(self.ret_instruction)
            self.nop_instruction = reverse_ends(self.nop_instruction)

        # generate regitster mapping (offset, size): name
        self.register_size_names = {}
        for k in self.registers:
            v = self.registers[k]

            # special hacks for X86 and AMD64 - don't translate register names to bp, sp, etc.
            if self.name in {'X86', 'AMD64'} and k in {'bp', 'sp', 'ip'}:
                continue

            if v in self.register_size_names and k not in self.register_names:
                continue
            self.register_size_names[v] = k

        # unicorn specific stuff
        if self.uc_mode is not None:
            if endness == Endness.BE:
                self.uc_mode -= _unicorn.UC_MODE_LITTLE_ENDIAN
                self.uc_mode += _unicorn.UC_MODE_BIG_ENDIAN
            self.uc_regs = { }
            # map register names to unicorn const
            for r in self.register_names.values():
                reg_name = self.uc_prefix + 'REG_' + r.upper()
                if hasattr(self.uc_const, reg_name):
                    self.uc_regs[r] = getattr(self.uc_const, reg_name)


    def copy(self):
        """
        Produce a copy of this instance of this arch.
        """
        new_arch = type(self)(self.memory_endness)
        new_arch.vex_archinfo = self.vex_archinfo.copy()

        return new_arch

    def __repr__(self):
        return '<Arch %s (%s)>' % (self.name, self.memory_endness[-2:])

    def __eq__(self, other):
        if not isinstance(other, Arch):
            return False
        return  self.name == other.name and \
                self.bits == other.bits and \
                self.memory_endness == other.memory_endness

    def __ne__(self, other):
        return not self == other

    def __getstate__(self):
        self._cs = None
        return self.__dict__

    def __setstate__(self, data):
        self.__dict__.update(data)

    def gather_info_from_state(self, state):
        info = {}
        for reg in self.persistent_regs:
            info[reg] = state.registers.load(reg)
        return info

    def prepare_state(self, state, info=None):
        if info is not None:
            # TODO: Only do this for PIC!
            for reg in self.persistent_regs:
                if reg in info:
                    state.registers.store(reg, info[reg])

        return state

    def get_default_reg_value(self, register):
        if register == 'sp':
            # Convert it to the corresponding register name
            registers = [r for r, v in self.registers.items() if v[0] == self.sp_offset]
            if len(registers) > 0:
                register = registers[0]
            else:
                return None
        for reg, val, _, _ in self.default_register_values:
            if reg == register:
                return val
        return None

    def struct_fmt(self, size=None):
        """
        Produce a format string for use in python's ``struct`` module.

        Optionally, the ``size`` parameter can specify the width of the int to store.
        """
        fmt = ""
        if size is None:
            size = self.bits

        if self.memory_endness == Endness.BE:
            fmt += ">"
        else:
            fmt += "<"

        if size == 64:
            fmt += "Q"
        elif size == 32:
            fmt += "I"
        elif size == 16:
            fmt += "H"
        elif size == 8:
            fmt += "B"
        else:
            raise ValueError("Invalid size: Must be a muliple of 8")

        return fmt



    @property
    def bytes(self):
        """
        The standard word size in bytes, calculated from the ``bits`` field
        """
        return self.bits // self.byte_width

    # e.g. sizeof['int'] = 4
    sizeof = {}

    @property
    def capstone(self):
        """
        A capstone instance for this arch
        """
        if self.cs_arch is None:
            raise ArchError("Arch %s does not support disassembly with capstone" % self.name)
        if self._cs is None:
            self._cs = _capstone.Cs(self.cs_arch, self.cs_mode)
            self._cs.detail = True
        return self._cs

    @property
    def unicorn(self):
        """
        A unicorn engine instance for this arch
        """
        if _unicorn is None or self.uc_arch is None:
            raise ArchError("Arch %s does not support with unicorn" % self.name)
        # always create a new unicorn instance
        return _unicorn.Uc(self.uc_arch, self.uc_mode)

    def translate_dynamic_tag(self, tag):
        try:
            return self.dynamic_tag_translation[tag]
        except KeyError:
            if isinstance(tag, (int, long)):
                l.error("Please look up and add dynamic tag type %#x for %s", tag, self.name)
            return tag

    def translate_symbol_type(self, tag):
        try:
            return self.symbol_type_translation[tag]
        except KeyError:
            if isinstance(tag, (int, long)):
                l.error("Please look up and add symbol type %#x for %s", tag, self.name)
            return tag

    def translate_register_name(self, offset, size=None):
        if size is not None:
            try:
                return self.register_size_names[(offset, size)]
            except KeyError:
                pass

        try:
            return self.register_names[offset]
        except KeyError:
            return str(offset)

    def get_register_offset(self, name):
        try:
            return self.registers[name][0]
        except:
            raise ValueError("Register %s does not exist!" % name)

    # Determined by watching the output of strace ld-linux.so.2 --list --inhibit-cache
    def library_search_path(self, pedantic=False):
        """
        A list of paths in which to search for shared libraries.
        """
        subfunc = lambda x: x.replace('${TRIPLET}', self.triplet).replace('${ARCH}', self.linux_name)
        path = ['/lib/${TRIPLET}/', '/usr/lib/${TRIPLET}/', '/lib/', '/usr/lib', '/usr/${TRIPLET}/lib/']
        if self.bits == 64:
            path.append('/usr/${TRIPLET}/lib64/')
            path.append('/usr/lib64/')
            path.append('/lib64/')
        elif self.bits == 32:
            path.append('/usr/${TRIPLET}/lib32/')
            path.append('/usr/lib32/')

        if pedantic:
            path = sum([[x + 'tls/${ARCH}/', x + 'tls/', x + '${ARCH}/', x] for x in path], [])
        return map(subfunc, path)

    # various names
    name = None
    vex_arch = None
    qemu_name = None
    ida_processor = None
    linux_name = None
    triplet = None

    # instruction stuff
    max_inst_bytes = None
    ret_instruction = b''
    nop_instruction = b''
    instruction_alignment = None

    # register ofsets
    ip_offset = None
    sp_offset = None
    bp_offset = None
    ret_offset = None
    lr_offset = None

    # whether or not VEX has ccall handlers for conditionals for this arch
    vex_conditional_helpers = False

    # memory stuff
    bits = None
    memory_endness = Endness.LE
    register_endness = Endness.LE
    stack_change = None

    # is it safe to cache IRSBs?
    cache_irsb = True

    branch_delay_slot = False

    function_prologs = set()
    function_epilogs = set()

    # Capstone stuff
    cs_arch = None
    cs_mode = None
    _cs = None

    # Unicorn stuff
    uc_arch = None
    uc_mode = None
    uc_const = None
    uc_prefix = None
    uc_regs = None

    call_pushes_ret = False
    initial_sp = 0x7fff0000

    # Difference of the stack pointer after a call instruction (or its equivalent) is executed
    call_sp_fix = 0

    stack_size = 0x8000000

    # Register information
    default_register_values = [ ]
    entry_register_values = { }
    default_symbolic_registers = [ ]
    registers = { }
    register_names = { }
    argument_registers = { }
    persistent_regs = [ ]
    concretize_unique_registers = set() # this is a list of registers that should be concretized, if unique, at the end of each block

    lib_paths = []
    reloc_s_a = []
    reloc_b_a = []
    reloc_s = []
    reloc_copy = []
    reloc_tls_mod_id = []
    reloc_tls_doffset = []
    reloc_tls_offset = []
    dynamic_tag_translation = {}
    symbol_type_translation = {}
    got_section_name = ''

    vex_archinfo = None


arch_id_map = []

all_arches = []

def register_arch(regexes, bits, endness, my_arch):
    """
    Register a new architecture.
    Architectures are loaded by their string name using ``arch_from_id()``, and
    this defines the mapping it uses to figure it out.
    Takes a list of regular expressions, and an Arch class as input.

    :param regexes: List of regular expressions (str or SRE_Pattern)
    :type regexes: list
    :param bits: The canonical "bits" of this architecture, ex. 32 or 64
    :type bits: int
    :param endness: The "endness" of this architecture.  Use Endness.LE, Endness.BE, or "any"
    :type endness: str
    :param Arch my_arch:
    :return: None
    """
    if not isinstance(regexes, list):
        raise TypeError("regexes must be a list")
    for rx in regexes:
        if not isinstance(rx, str) and not isinstance(rx,re._pattern_type):
            raise TypeError("Each regex must be a string or compiled regular expression")
        try:
            re.compile(rx)
        except:
            raise ValueError('Invalid Regular Expression %s' % rx)
    #if not isinstance(my_arch,Arch):
    #    raise TypeError("Arch must be a subclass of archinfo.Arch")
    if not isinstance(bits, int):
        raise TypeError("Bits must be an int")
    if not isinstance(endness,str):
        if endness != Endness.BE and endness != Endness.LE and endness != "any":
            raise TypeError("Endness must be Endness.BE, Endness.LE, or 'any'")
    arch_id_map.append((regexes, bits, endness, my_arch))
    if endness == 'any':
        all_arches.append(my_arch(Endness.BE))
        all_arches.append(my_arch(Endness.LE))
    else:
        all_arches.append(my_arch(endness))


def arch_from_id(ident, endness='any', bits=''):
    """
    Take our best guess at the arch referred to by the given identifier, and return an instance of its class.

    You may optionally provide the ``endness`` and ``bits`` parameters (strings) to help this function out.
    """
    if bits == 64 or (isinstance(bits, str) and '64' in bits):
        bits = 64
    elif isinstance(bits,str) and '32' in bits:
        bits = 32
    elif not bits and '64' in ident:
        bits = 64
    elif not bits and '32' in ident:
        bits = 32

    endness = endness.lower()
    if 'lit' in endness:
        endness = Endness.LE
    elif 'big' in endness:
        endness = Endness.BE
    elif 'lsb' in endness:
        endness = Endness.LE
    elif 'msb' in endness:
        endness = Endness.BE
    elif 'le' in endness:
        endness = Endness.LE
    elif 'be' in endness:
        endness = Endness.BE
    elif 'l' in endness:
        endness = 'unsure'
    elif 'b' in endness:
        endness = 'unsure'
    else:
        endness = 'unsure'
    ident = ident.lower()
    cls = None
    aendness = ""
    for arxs, abits, aendness, acls in arch_id_map:
        found_it = False
        for rx in arxs:
            if re.match(rx, ident):
                found_it = True
                break
        if not found_it:
            continue
        if bits and bits != abits:
            continue
        if aendness == 'any' or endness == aendness or endness == 'unsure':
            cls = acls
            break
    if not cls:
        raise RuntimeError("Can't find architecture info for architecture %s with %s bits and %s endness" % (ident, repr(bits), endness))
    if endness == 'unsure':
        if aendness == 'any':
            # We really don't care, use default
            return cls()
        else:
            # We're expecting the ident to pick the endness.
            # ex. 'armeb' means obviously this is Iend_BE
            return cls(aendness)
    else:
        return cls(endness)


def reverse_ends(string):
    ise = 'I'*(len(string)//4)
    return _struct.pack('>' + ise, *_struct.unpack('<' + ise, string))

def get_host_arch():
    """
    Return the arch of the machine we are currently running on.
    """
    return arch_from_id(_platform.machine())
